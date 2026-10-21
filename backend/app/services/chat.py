"""Диалог SneakChat: контекст, уточнения, ослабление условий и ответы.

Движок держит условия диалога в состоянии сессии и работает по принципу «продавец, а не
фильтр»: сначала пытается показать товары, задаёт не больше одного короткого вопроса,
меняет и снимает условия по одной фразе и честно сообщает, когда условие ослаблено.

Слой диалога специфичен для кроссовок: оси (назначение, размер, цвет), тексты ответов
и порядок ослабления условий заданы конфигурацией каталога, а хранение и поиск —
универсальное ядро `catalog.py`.
"""

from collections import defaultdict
import logging
import re
import os

from sqlalchemy.orm import Session

from ..ai.grounding import verify_against_filters, verify_products, verify_reasons, verify_reply
from ..ai.providers import ProviderUnavailable, get_provider
from ..ai.tools import recommend_products, search_products
from ..ai.validation import validate_analysis
from ..catalog import catalogue_brands
from ..catalog_rules import (
    product_fact_question, GREETINGS,
    MAX_PRICE_MARKERS, MIN_PRICE_MARKERS, SIZE_MARKERS, excluded_size_numbers,
    BRAND_CANONICAL, COLOR_CANONICAL, COLOR_LABELS, EVIDENCE_MIN_WORD_LENGTH, FEATURE_CANONICAL,
    FEATURE_LABELS, GENDER_ALIAS_MAP, GENDER_CANONICAL, GENDER_LABELS, MODEL_TRUSTED_WORDS, MODEL_VALUE_STEMS, match_alias,
    NUMERIC_FILTER_KEYS, STOP_WORDS,
    UNKNOWN_CATALOGUE_LABELS, USE_CASE_CANONICAL, USE_CASE_LABELS, ABSENT_PROPERTY_LABELS, alias_owners,
    detect_absent_properties, detect_negations, detect_removals, detect_out_of_range_size,
    detect_unknown_catalogue_values, implied_value, is_recommendation_request, is_similar_request,
    NEW_SEARCH_PHRASES, SNEAKER_CONFIG, shopping_intent, size_axis_bounds,
)
from .currency import convert_amount, get_rates
from ..schemas import AIAnalysis, sanitize_price
from .assistant import parse_request
from .chat_sessions import load_session_state, save_session_state
from .dialogue_state import discovery_question, prepare_turn, record_reply, selection_help_request
from .products import get_product, serialize_product
from .request_safety import (
    catalog_model_filters, comparison_model_names, conflict_response,
    measurement_response, missing_model_response, update_measurement,
)

_sessions: dict[str, dict] = defaultdict(lambda: {"filters": {}, "products": [], "history": [], "shown_product_ids": []})

_PRICE_KEYS = ("min_price", "max_price")
_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "UAH": "₴"}
# Значения модели без опоры на собственный признак в тексте живут один ход: иначе выдуманный
# бюджет или размер, подтверждённый случайной цифрой, сузил бы все следующие выборки.
_TURN_SCOPED_KEYS = ("size", "min_price", "max_price")
# Сколько моделей показываем за один ход и насколько «похожими» считаем соседние цены.
_RESULT_LIMIT = 3
_SIMILAR_PRICE_BAND = 0.3
# Смысловые условия: их сохраняем даже в самом широком ослаблении выдачи.
_SOFT_KEPT_KEYS = ("size", "use_case", "feature", "gender")
# Отрицания — тоже условия пользователя, поэтому ослабление их не отменяет: «не Nike» значит
# «покажи что угодно, кроме Nike», и широкий ответ обязан это уважать.
_SOFT_KEPT_EXCLUSIONS = ("exclude_brand", "exclude_attributes")
# Оси, у которых пользователь может исключить конкретное значение.
_EXCLUSION_AXES = ("brand", "color", "use_case", "feature", "gender", "size")
# Кнопки ослабления в честном «ничего не нашлось»: формулировки берутся из словаря фраз снятия
# условий (`REMOVAL_PHRASES`), поэтому каждая кнопка действительно меняет запрос.
_RELAX_CHIPS = (
    ("brand", {"ru": "Бренд не важен", "uk": "Бренд не важливий"}),
    ("color", {"ru": "Покажи без ограничения по цвету", "uk": "Покажи без обмеження за кольором"}),
    ("price", {"ru": "Без ограничения по цене", "uk": "Без обмеження за ціною"}),
    ("size", {"ru": "Размер не важен", "uk": "Розмір не враховуй"}),
)


def _context_filters(filters: dict, turn_scoped_keys: set[str]) -> dict:
    """Фильтры, которые переносятся в контекст следующего сообщения.

    Значение, которое предложила только модель (его нет в тексте пользователя), действует
    один ход и не переносится дальше. Значения, найденные парсером, переносятся как обычно.
    """
    return {key: value for key, value in filters.items() if key not in turn_scoped_keys}


def _soft_preference_updates(message: str, filters: dict, display_currency: str) -> dict:
    """Separate explicit preferences from hard catalog filters without inferring buyer traits."""
    lowered = message.lower()
    result: dict = {}
    if re.search(r"\b(?:желательно|предпочтительно|предпочитаю|бажано|переважно)\b", lowered):
        if filters.get("brand"):
            result["brand"] = filters.pop("brand")
    approximate = re.search(
        r"\b(?:примерно|около|где-то|приблизно|близько|десь)\s*(?:за\s*)?\$?\s*(\d+(?:[.,]\d+)?)",
        lowered,
    )
    if approximate:
        # Approximate price is a ranking hint, not a ceiling. Explicit limits in the same
        # message are left intact by parse_request and therefore are never relaxed here.
        explicit_limit = re.search(
            r"\b(?:до|не дороже|не дорожче|максимум|лимит|ліміт|не больше|не більше|в пределах|у межах)\b",
            lowered,
        )
        if not explicit_limit:
            result["price_target"] = {
                "amount": float(approximate.group(1).replace(",", ".")),
                "currency": filters.get("currency") or display_currency,
            }
            filters.pop("min_price", None)
            filters.pop("max_price", None)
            filters.pop("currency", None)
    if re.search(r"\b(?:премиум|премиальные|преміальний|преміальні|дорогие|дорогой|дорогая|дорогі)\b", lowered):
        result["price_order"] = "high"
    return result


def _rank_soft_preferences(products: list[dict], preferences: dict) -> list[dict]:
    brand = str(preferences.get("brand") or "").casefold()
    target = preferences.get("price_target") or {}
    amount = target.get("amount") if isinstance(target, dict) else None
    currency = str(target.get("currency") or "") if isinstance(target, dict) else ""
    order = preferences.get("price_order")

    def rank(product):
        product_currency = str(product.get("base_currency") or product.get("currency") or "")
        price = float(product.get("price", 0))
        target_distance = abs(price - float(amount)) if amount is not None and currency == product_currency else float("inf")
        price_rank = -price if order == "high" else 0
        return (
            0 if not brand or str(product.get("brand", "")).casefold() == brand else 1,
            target_distance,
            price_rank,
            product.get("id", 0),
        )

    return sorted(products, key=rank)


COPY = {
    "ru": {
        "brand": "бренд", "color": "цвет", "size": "размер", "up_to": "до", "from_price": "от",
        "budget": "бюджет", "relax_or": "или",
        "ask_size": "Какой EU-размер вам нужен?",
        "ask_size_with_purpose": "Выбираем {purpose}. Какой EU-размер вам нужен?",
        "ask_goal": "Что для вас важно при выборе кроссовок?",
        "ask_open": "Что для вас важно при выборе кроссовок?",
        "ask_fit_followup": "В каталоге нет данных о ширине и посадке {product}, поэтому не могу сказать, будет ли она жать. Хотите, покажу цену и доступные EU-размеры?",
        "ask_catalog_attribute": "Какую модель, цвет или бюджет искать?",
        "price_target_currency_unavailable": "Ориентир {amount:g} {currency} сохранён, но цены подходящих товаров указаны в другой валюте; без подтверждённого курса сравнить их нельзя. ",
        "greeting": "Здравствуйте! Чем могу вам помочь?",
        "no_results": "По условиям «{summary}» подходящих моделей в наличии нет. Можно ослабить один из фильтров.",
        "no_results_relax": "По условиям «{summary}» подходящих моделей в наличии нет. Можно ослабить: {relax}.",
        "no_results_size": "По условиям «{summary}» подходящих моделей в наличии нет. Ближайшие доступные размеры: {sizes}. Можно ослабить: {relax}.",
        "cheaper_none": "Дешевле показанного по этим условиям моделей нет. ",
        "nearby_size": "Точного размера {size} в наличии по этим условиям нет. Ниже — реальные варианты с ближайшим доступным размером: {sizes}.",
        "over_budget": "В указанном бюджете вариантов нет. Ниже — ближайшие реальные модели немного выше бюджета.",
        "relaxed_color": "Точных моделей с условиями «{summary}» нет. Ниже — {count} по тем же условиям, но в других цветах.",
        "relaxed_brand": "С этими условиями подходящего бренда не нашлось. Ниже — {count} другого бренда с тем же размером и бюджетом.",
        "more": "Показываю ещё подходящие варианты.",
        "no_more": "По этим условиям больше моделей нет. Можно изменить цвет, бренд или бюджет.",
        "found": "Нашёл {count} по условиям «{summary}».",
        "stock": "Проверил каталог: подходящие варианты и доступные размеры показаны ниже.",
        "recommend_data": "Посмотрите {product}: {reasons}.",
        "similar": "Похожие на {anchor}: то же назначение и близкая цена — {count}.",
        "similar_none": "Пока не с чем сравнивать: сначала подберу варианты по вашему запросу.",
        "broadest": "Точных моделей по условиям «{summary}» нет. Бренд, цвет и цену не учитываю: {count}, {kept}.",
        "kept_with": "оставляю условия:",
        "kept_join": ", ",
        "kept_size": "размер",
        "kept_use_case": "назначение",
        "kept_feature": "особенности",
        "kept_gender": "пол",
        "kept_nothing": "оставляю весь каталог",
        "absent": "В каталоге нет {label} — подбираю по бренду, назначению, цвету, размеру и бюджету. ",
        "found_all": "Показываю модели из каталога: {count}.",
        "size_dropped": "Хорошо, размер не учитываю.",
        "not": "не",
        "conflict": "Условие «{label}» вы назвали и одновременно исключили, поэтому учитывать его не буду. ",
        "size_out_of_range": "Размера {size:g} в каталоге нет: доступны размеры {minimum:g}–{maximum:g}. Какой размер вам нужен?",
        "unknown_brand": "Бренда {value} в каталоге нет — есть {available}. Могу подобрать из того, что есть. ",
        "unknown_value": "В каталоге нет {label}. Могу подобрать из того, что есть. ",
        "unknown_model": "Именно {label} сейчас нет в каталоге — покажу близкие варианты этого бренда. ",
        "kept_excluded": "исключение",
        "broadest_excluded": "Точных моделей по условиям «{summary}» нет. Бренд, цвет и цену не учитываю: {count}, {kept}. Ваше исключение сохраняется: {excluded}.",
        "reason_excluded": "не {label}",
        "grounding_blocked": "Не удалось подтвердить данные этих моделей, поэтому не показываю их. Уточните запрос — подберу заново.",
        "reason_size": "размер {size} в наличии",
        "reason_size_nearby": "ближайший доступный размер",
        "reason_budget": "укладывается в бюджет до {symbol}{price}",
        "reason_stock": "есть в наличии",
        "reason_catalog": "модель из демонстрационного каталога",
        "follow": ["Покажи ещё", "Покажи подешевле", "Другой цвет", "Похожие", "Другой бренд"],
        "empty": ["Покажи без ограничения по цвету", "До $150", "Покажи все"],
        "goals": ["Для прогулок", "Для бега", "Для зала", "Для школы"],
    },
    "uk": {
        "brand": "бренд", "color": "колір", "size": "розмір", "up_to": "до", "from_price": "від",
        "budget": "бюджет", "relax_or": "або",
        "ask_size": "Який EU-розмір вам потрібен?",
        "ask_size_with_purpose": "Обираємо {purpose}. Який EU-розмір вам потрібен?",
        "ask_goal": "Що для вас важливо при виборі кросівок?",
        "ask_open": "Що для вас важливо при виборі кросівок?",
        "ask_fit_followup": "У каталозі немає даних про ширину та посадку {product}, тому не можу сказати, чи тиснутиме вона. Показати ціну й доступні EU-розміри?",
        "ask_catalog_attribute": "Яку модель, колір або бюджет шукати?",
        "price_target_currency_unavailable": "Орієнтир {amount:g} {currency} збережено, але ціни відповідних товарів указано в іншій валюті; без підтвердженого курсу порівняти їх не можу. ",
        "greeting": "Вітаю! Чим можу вам допомогти?",
        "no_results": "За умовами «{summary}» відповідних моделей у наявності немає. Можна послабити один із фільтрів.",
        "no_results_relax": "За умовами «{summary}» відповідних моделей у наявності немає. Можна послабити: {relax}.",
        "no_results_size": "За умовами «{summary}» відповідних моделей у наявності немає. Найближчі доступні розміри: {sizes}. Можна послабити: {relax}.",
        "cheaper_none": "Дешевше за показане за цими умовами моделей немає. ",
        "nearby_size": "Точного розміру {size} у наявності за цими умовами немає. Нижче — реальні варіанти з найближчим доступним розміром: {sizes}.",
        "over_budget": "У вказаному бюджеті варіантів немає. Нижче — найближчі реальні моделі трохи дорожче за бюджет.",
        "relaxed_color": "Точних моделей з умовами «{summary}» немає. Нижче — {count} за тими самими умовами, але в інших кольорах.",
        "relaxed_brand": "За цими умовами відповідного бренду не знайшлося. Нижче — {count} іншого бренду з тим самим розміром і бюджетом.",
        "more": "Показую ще відповідні варіанти.",
        "no_more": "За цими умовами більше моделей немає. Можна змінити колір, бренд або бюджет.",
        "found": "Знайшов {count} за умовами «{summary}».",
        "stock": "Перевірив каталог: відповідні варіанти й доступні розміри показані нижче.",
        "recommend_data": "Подивіться {product}: {reasons}.",
        "similar": "Схожі на {anchor}: те саме призначення і близька ціна — {count}.",
        "similar_none": "Поки немає з чим порівнювати: спершу підберу варіанти за вашим запитом.",
        "broadest": "Точних моделей за умовами «{summary}» немає. Бренд, колір і ціну не враховую: {count}, {kept}.",
        "kept_with": "залишаю умови:",
        "kept_join": ", ",
        "kept_size": "розмір",
        "kept_use_case": "призначення",
        "kept_feature": "особливості",
        "kept_gender": "стать",
        "kept_nothing": "залишаю весь каталог",
        "absent": "У каталозі немає {label} — підбираю за брендом, призначенням, кольором, розміром і бюджетом. ",
        "found_all": "Показую моделі з каталогу: {count}.",
        "size_dropped": "Гаразд, розмір не враховую.",
        "not": "не",
        "conflict": "Умову «{label}» ви назвали й одночасно виключили, тому враховувати її не буду. ",
        "size_out_of_range": "Розміру {size:g} у каталозі немає: доступні розміри {minimum:g}–{maximum:g}. Який розмір вам потрібен?",
        "unknown_brand": "Бренду {value} у каталозі немає — є {available}. Можу підібрати з того, що є. ",
        "unknown_value": "У каталозі немає {label}. Можу підібрати з того, що є. ",
        "unknown_model": "Саме {label} зараз немає в каталозі — покажу близькі варіанти цього бренду. ",
        "kept_excluded": "виняток",
        "broadest_excluded": "Точних моделей за умовами «{summary}» немає. Бренд, колір і ціну не враховую: {count}, {kept}. Ваш виняток зберігається: {excluded}.",
        "reason_excluded": "не {label}",
        "grounding_blocked": "Не вдалося підтвердити дані цих моделей, тому не показую їх. Уточніть запит — підберу заново.",
        "reason_size": "розмір {size} у наявності",
        "reason_size_nearby": "найближчий доступний розмір",
        "reason_budget": "вкладається в бюджет до {symbol}{price}",
        "reason_stock": "є в наявності",
        "reason_catalog": "модель із демонстраційного каталогу",
        "follow": ["Покажи ще", "Покажи дешевше", "Інший колір", "Схожі", "Інший бренд"],
        "empty": ["Покажи без обмеження за кольором", "До $150", "Покажи всі"],
        "goals": ["Для прогулянок", "Для бігу", "Для залу", "Для школи"],
    },
}


# Код атрибута каталога → ось диалога. Нужно, чтобы подписи исключений брались из словаря,
# а не из кода атрибута: пользователю говорят «не белый», а не «не color».
_AXIS_BY_ATTRIBUTE_CODE: dict[str, str] = {
    **{spec.code: spec.code for spec in SNEAKER_CONFIG.attributes},
    **{code: axis for axis, code in SNEAKER_CONFIG.legacy_keys.items()},
}
_ATTRIBUTE_CODE_BY_AXIS: dict[str, str] = {axis: code for code, axis in _AXIS_BY_ATTRIBUTE_CODE.items()}


def _value_label(axis: str, value: object, language: str = "ru") -> str:
    """Как назвать значение в ответе: словами из словаря, если словарь его знает."""
    if axis == "color":
        code = str(value).lower()
        return COLOR_LABELS[language].get(code, code)
    if axis == "use_case":
        return USE_CASE_LABELS[language].get(str(value), str(value))
    if axis == "feature":
        return FEATURE_LABELS[language].get(str(value), str(value))
    if axis == "gender":
        return GENDER_LABELS[language].get(str(value), str(value))
    if axis == "size":
        return f"{float(value):g}"
    return str(value)


def _exclusion_pairs(filters: dict) -> list[tuple[str, object]]:
    """Исключения пользователя в виде пар «ось, значение»."""
    pairs: list[tuple[str, object]] = [("brand", value) for value in filters.get("exclude_brand") or []]
    for code, value in (filters.get("exclude_attributes") or {}).items():
        axis = _AXIS_BY_ATTRIBUTE_CODE.get(str(code), str(code))
        values = value if isinstance(value, (list, tuple)) else [value]
        pairs.extend((axis, item) for item in values)
    return pairs


def _exclusion_summary(filters: dict, language: str = "ru") -> str:
    """Исключения в виде готовой фразы: «не Nike, не белый»."""
    copy = COPY[language]
    return ", ".join(f"{copy['not']} {_value_label(axis, value, language)}" for axis, value in _exclusion_pairs(filters))


def _exclusion_conditions(negations: dict[str, list]) -> tuple[list[str], dict[str, list]]:
    """Превращает распознанные отрицания в условия ядра каталога.

    Бренд хранится отдельной колонкой, а всё остальное — атрибутами, поэтому и возвращаются
    две части: исключаемые бренды и исключаемые значения атрибутов.
    """
    brands = [str(item) for item in negations.get("brand") or []]
    attributes: dict[str, list] = {}
    for axis in ("color", "use_case", "feature", "gender", "size"):
        values = negations.get(axis) or []
        if values:
            attributes[_ATTRIBUTE_CODE_BY_AXIS[axis]] = list(values)
    return brands, attributes


def _drop_included_value(filters: dict, axis: str, value: object) -> bool:
    """Убирает значение из положительного условия. True — если условие действительно изменилось.

    Нужно для противоречия «Nike, но не Nike»: сказанное и тут же исключённое значение не
    учитывается, иначе один и тот же товар обязан быть и в выдаче, и вне её.
    """
    included = filters.get(axis)
    if included is None:
        return False
    target = str(value).lower()
    if isinstance(included, (list, tuple)):
        kept = [item for item in included if str(item).lower() != target]
        if len(kept) == len(included):
            return False
        if kept:
            filters[axis] = kept
        else:
            filters.pop(axis, None)
        return True
    if str(included).lower() != target:
        return False
    filters.pop(axis, None)
    return True


def _merge_exclusions(previous: dict, exclusions: dict) -> dict:
    """Дополняет уже собранные исключения новыми: «не белые», потом «не чёрные» — оба."""
    merged = {code: (list(values) if isinstance(values, (list, tuple)) else [values]) for code, values in previous.items()}
    for code, values in exclusions.items():
        current = merged.setdefault(code, [])
        for item in values:
            if item not in current:
                current.append(item)
    return merged


def _product_axis_value(product: dict, axis: str):
    """Фактическое значение оси у товара — из данных каталога, а не из запроса."""
    if axis == "size":
        return [float(size) for size in product.get("available_sizes") or []]
    if axis == "feature":
        return [str(item) for item in product.get("features") or []]
    if axis == "brand":
        return product.get("brand")
    if axis == "color":
        return product.get("color")
    if axis == "use_case":
        return product.get("use_case")
    if axis == "gender":
        return product.get("gender")
    return None


def _exclusion_holds(product: dict, axis: str, value: object) -> bool:
    """Действительно ли товар не подпадает под исключение.

    Проверка идёт по данным товара, поэтому пояснение «не белый» появляется только тогда,
    когда цвет товара в базе и правда другой.
    """
    actual = _product_axis_value(product, axis)
    if actual is None:
        return False
    if isinstance(actual, list):
        if axis == "size":
            return float(value) not in actual
        return str(value).lower() not in {str(item).lower() for item in actual}
    return str(actual).lower() != str(value).lower()


def _filter_summary(filters: dict, language: str = "ru") -> str:
    copy = COPY[language]
    symbol = _CURRENCY_SYMBOLS.get(filters.get("currency", "USD"), "$")
    parts = []
    if filters.get("brand"):
        parts.append(str(filters["brand"]))
    if filters.get("color"):
        color = str(filters["color"]).lower()
        parts.append(f"{copy['color']} {COLOR_LABELS[language].get(color, color)}")
    if filters.get("size"):
        parts.append(f"{copy['size']} {filters['size']:g}")
    if filters.get("use_case"):
        use_case = str(filters["use_case"])
        parts.append(USE_CASE_LABELS[language].get(use_case, use_case))
    if filters.get("feature"):
        features = filters["feature"] if isinstance(filters["feature"], (list, tuple)) else [filters["feature"]]
        parts.extend(FEATURE_LABELS[language].get(str(code), str(code)) for code in features)
    if filters.get("min_price"):
        parts.append(f"{copy['from_price']} {symbol}{filters['min_price']:g}")
    if filters.get("max_price"):
        parts.append(f"{copy['up_to']} {symbol}{filters['max_price']:g}")
    # Исключения называются вместе с условиями: «до $120, не белый» — это тоже запрос.
    exclusions = _exclusion_summary(filters, language)
    if exclusions:
        parts.append(exclusions)
    return ", ".join(parts)


def _variant_count(count: int, language: str = "ru") -> str:
    if language == "uk":
        if count % 10 == 1 and count % 100 != 11:
            return f"{count} відповідний варіант"
        if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
            return f"{count} відповідні варіанти"
        return f"{count} відповідних варіантів"
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} подходящий вариант"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return f"{count} подходящих варианта"
    return f"{count} подходящих вариантов"


def _suggestions(has_products: bool, needs_size: bool, language: str = "ru") -> list[str]:
    if needs_size:
        return ["36", "38", "42", "43"]
    if has_products:
        return COPY[language]["follow"]
    return COPY[language]["empty"]


def _is_more_request(message: str) -> bool:
    return message.strip(" ?!.") in {"другие", "інші"} or any(phrase in message for phrase in ("покажи еще", "покажи ещё", "еще варианты", "ещё варианты", "другие варианты", "покажи ще", "ще варіанти", "інші варіанти"))


def _is_greeting(message: str) -> bool:
    normalized = " ".join(re.findall(r"[\w’'-]+", message.casefold()))
    return normalized in GREETINGS


def _nearest_size_products(db: Session, filters: dict) -> tuple[list[dict], list[float]]:
    # Размер мог быть снят фразой «размер не учитывай»: тогда ближайший размер не ищем.
    if filters.get("size") is None:
        return [], []
    requested_size = float(filters["size"])
    broad_filters = {key: value for key, value in filters.items() if key != "size"}
    candidates = recommend_products(db, limit=10, **broad_filters)
    ranked: list[tuple[float, float, int, dict, float]] = []
    for product in candidates:
        available_sizes = product["available_sizes"]
        if not available_sizes:
            continue
        closest = min(available_sizes, key=lambda size: abs(float(size) - requested_size))
        ranked.append((abs(float(closest) - requested_size), product["price"], product["id"], product, float(closest)))
    ranked.sort(key=lambda item: item[:3])
    nearby = [item for item in ranked if item[0] <= 1]
    return [item[3] for item in nearby[:_RESULT_LIMIT]], sorted({item[4] for item in nearby})


def _size_options(db: Session, filters: dict, limit: int = 4) -> list[float]:
    """Ближайшие доступные размеры по тем же условиям, но без размера.

    Нужны, когда запрошенного размера нет ни у одной подходящей модели: покупателю важно
    увидеть, какие размеры реально есть, а не общее «ничего не найдено».
    """
    if not filters.get("size"):
        return []
    requested = float(filters["size"])
    broad_filters = {key: value for key, value in filters.items() if key not in {"size", "name_query"}}
    candidates = recommend_products(db, limit=10, **broad_filters)
    sizes = {float(size) for product in candidates for size in product.get("available_sizes") or []}
    if not sizes:
        return []
    return sorted(sizes, key=lambda size: (abs(size - requested), size))[:limit]


def _relax_phrase(filters: dict, language: str = "ru") -> str:
    """Что именно можно ослабить в текущих условиях — по осям, которые пользователь назвал."""
    copy = COPY[language]
    names: list[str] = []
    for key, label in (("brand", "brand"), ("color", "color"), ("max_price", "budget"),
                       ("min_price", "budget"), ("size", "size")):
        if filters.get(key) is not None and copy.get(label) not in names:
            names.append(copy[label])
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{copy['kept_join'].join(names[:-1])} {copy['relax_or']} {names[-1]}"


def _relax_suggestions(filters: dict, language: str = "ru") -> list[str]:
    """Кнопки ослабления: только те условия, которые в этом запросе действительно есть.

    Формулировки совпадают с фразами снятия условий в словаре, поэтому каждая кнопка
    проходит доменный gate и меняет выдачу — а не повторяет прежний ответ.
    """
    chips: list[str] = []
    for axis, labels in _RELAX_CHIPS:
        present = (
            filters.get("max_price") is not None or filters.get("min_price") is not None
            if axis == "price" else bool(filters.get(axis))
        )
        if present:
            chips.append(labels[language])
    return chips or list(COPY[language]["empty"])


def _near_budget_products(db: Session, filters: dict) -> list[dict]:
    if filters.get("max_price") is None:
        return []
    ceiling = float(filters["max_price"])
    broad_filters = {key: value for key, value in filters.items() if key != "max_price"}
    candidates = recommend_products(db, limit=10, **broad_filters)
    return [product for product in candidates if product["price"] <= ceiling + 40][:_RESULT_LIMIT]


def _similar_filters(anchor: dict, current: dict, language: str = "ru") -> dict:
    """Условия для «покажи похожие»: то же назначение и близкая цена, без бренда и цвета.

    Смысловые условия пользователя (размер, назначение, теги, пол) сохраняются: «похожие»
    не должны молча показывать товар без нужного размера. Меняются только бренд, цвет и цена.
    """
    filters: dict[str, object] = {key: value for key, value in current.items() if key in _SOFT_KEPT_KEYS}
    # Исключения переносятся: «похожие» не должны показывать то, что пользователь отверг.
    filters.update({key: value for key, value in current.items() if key in _SOFT_KEPT_EXCLUSIONS})
    if not filters.get("use_case") and anchor.get("use_case"):
        filters["use_case"] = anchor["use_case"]
    # Keep the customer's category if given; do not exclude Unisex alternatives
    # solely because the previously shown product was labelled Men or Women.
    price = float(anchor.get("price") or 0)
    if anchor.get("currency"):
        filters["currency"] = anchor["currency"]
    if price:
        filters["min_price"] = round(price * (1 - _SIMILAR_PRICE_BAND))
        filters["max_price"] = round(price * (1 + _SIMILAR_PRICE_BAND))
    return filters


def _kept_conditions_phrase(filters: dict, language: str = "ru") -> str:
    """Что именно сохранилось в широком ослаблении — без упоминания того, чего не было."""
    copy = COPY[language]
    kept = []
    if filters.get("size"):
        kept.append(copy["kept_size"])
    if filters.get("use_case"):
        kept.append(copy["kept_use_case"])
    if filters.get("feature"):
        kept.append(copy["kept_feature"])
    if filters.get("gender"):
        kept.append(copy["kept_gender"])
    if _exclusion_pairs(filters):
        kept.append(copy["kept_excluded"])
    if not kept:
        return copy["kept_nothing"]
    return f"{copy['kept_with']} " + copy["kept_join"].join(kept)


def _match_reasons(product: dict, filters: dict, language: str, nearest: bool = False) -> list[str]:
    """Почему товар подходит — только из данных каталога и фактически применённых условий."""
    copy = COPY[language]
    reasons: list[str] = []
    if filters.get("brand") and str(product.get("brand", "")).lower() == str(filters["brand"]).lower():
        reasons.append(f"{copy['brand']} {product['brand']}")
    if filters.get("color") and product.get("color") and str(product["color"]).lower() == str(filters["color"]).lower():
        color = str(product["color"]).lower()
        reasons.append(f"{copy['color']} {COLOR_LABELS[language].get(color, color)}")
    if filters.get("size"):
        requested = float(filters["size"])
        if requested in [float(size) for size in product.get("available_sizes", [])]:
            reasons.append(copy["reason_size"].format(size=f"{requested:g}"))
        elif nearest:
            reasons.append(copy["reason_size_nearby"])
    if filters.get("use_case") and product.get("use_case") == filters["use_case"]:
        use_case = str(product["use_case"])
        reasons.append(USE_CASE_LABELS[language].get(use_case, use_case))
    if filters.get("feature"):
        wanted = filters["feature"] if isinstance(filters["feature"], (list, tuple)) else [filters["feature"]]
        for code in wanted:
            if code in (product.get("features") or []):
                reasons.append(FEATURE_LABELS[language].get(str(code), str(code)))
    if filters.get("gender") and str(product.get("gender", "")).lower() == str(filters["gender"]).lower():
        gender = str(product["gender"])
        reasons.append(GENDER_LABELS[language].get(gender, gender))
    budget_currency = filters.get("currency") or product.get("currency", "USD")
    budget_price = None
    if filters.get("max_price") is not None:
        source_currency = product.get("base_currency", product.get("currency", "USD"))
        snapshot = get_rates() if source_currency != budget_currency else None
        rates = snapshot.get("rates") if snapshot and snapshot.get("available") else None
        budget_price = convert_amount(
            float(product.get("base_price", product.get("price", 0)) or 0), source_currency, budget_currency, rates,
        )
    if filters.get("max_price") is not None and budget_price is not None and budget_price <= float(filters["max_price"]):
        symbol = _CURRENCY_SYMBOLS.get(budget_currency, "$")
        reasons.append(copy["reason_budget"].format(symbol=symbol, price=f"{float(filters['max_price']):g}"))
    # Исключение подтверждается данными товара: «не белый» пишется только тогда, когда цвет
    # в базе действительно другой, а не потому, что так решил фильтр.
    for axis, value in _exclusion_pairs(filters):
        if _exclusion_holds(product, axis, value):
            reasons.append(copy["reason_excluded"].format(label=_value_label(axis, value, language)))
    if not reasons:
        reasons.append(f"{copy['reason_stock']} ({product.get('total_stock', 0)})")
    return reasons


def _with_reasons(products: list[dict], filters: dict, language: str, nearest: bool = False) -> list[dict]:
    for product in products:
        product["match_reasons"] = _match_reasons(product, filters, language, nearest=nearest)
    return products


def _normalize_text(value: object) -> str:
    """Сравнение подтверждения с текстом не должно зависеть от регистра, запятых и пробелов."""
    return " ".join(str(value).lower().replace(",", ".").split())


def _evidence_is_usable(key: str, value: object, phrase: str, text: str, size_phrase: bool = False) -> bool:
    """Годится ли подтверждение для этого фильтра.

    Требования: фраза присутствует в сообщении; для размера и цены содержит цифру;
    для категорий состоит хотя бы из одного содержательного слова, по словарю не относится
    к другому фильтру и, если словарь знает эту формулировку, указывает на то же значение.
    Поэтому одно служебное слово («и», «для») или чужой признак («белые» для бренда,
    «бел» для чёрного цвета) подтверждением быть не может.
    """
    if not phrase or phrase not in text:
        return False
    if key in NUMERIC_FILTER_KEYS:
        # Число подтверждает значение, только если оно ровно то же и фраза говорит о той же оси.
        # Иначе цена «120» подтверждала бы размер 44, «3 пары» — бюджет 3, а «до 120» — нижнюю
        # границу цены.
        numbers = {float(item.replace(",", ".")) for item in re.findall(r"\d+(?:[.,]\d+)?", phrase)}
        if not any(abs(number - float(value)) < 0.001 for number in numbers):
            return False
        if float(value) in excluded_size_numbers(text):
            # Число в сообщении означает возраст или количество: ни размером, ни бюджетом
            # такое число не подтверждается.
            return False
        if key == "min_price":
            # Голое число никогда не означает «не дешевле»: нужен явный признак нижней границы.
            return any(marker in phrase for marker in MIN_PRICE_MARKERS)
        if key == "size":
            return size_phrase or any(marker in phrase for marker in SIZE_MARKERS) or re.fullmatch(r"\d+(?:[.,]\d+)?", phrase) is not None
        # Верхняя граница: голое число или явный признак бюджета, но не количество чего-то.
        return re.fullmatch(r"\d+(?:[.,]\d+)?", phrase) is not None or any(marker in phrase for marker in MAX_PRICE_MARKERS)
    content_words = [
        word for word in phrase.split()
        if len(word) >= EVIDENCE_MIN_WORD_LENGTH and word not in STOP_WORDS and any(character.isalpha() for character in word)
    ]
    if not content_words:
        return False
    known = implied_value(key, phrase)
    if known is not None:
        # Словарь знает формулировку: она обязана указывать именно на это значение.
        return known == value
    # Словарь формулировку не знает. Тогда она принимается только из списка доверенных
    # и только если подразумевает то же значение: иначе любое слово сообщения подтвердило бы
    # любое значение и модель могла бы навязать фильтр, которого пользователь не называл.
    trusted = MODEL_TRUSTED_WORDS.get(key, {})
    # Формулировка ищется по началу слова, а не по началу фразы: «для пробежек» — это про
    # назначение, хотя фраза начинается со служебного слова.
    if match_alias(phrase, trusted) != value:
        return False
    return alias_owners(phrase) <= {key}


def _is_a_price_number(value: object, parsed: dict) -> bool:
    """Совпадает ли число с бюджетом, прочитанным из сообщения."""
    for key in ("min_price", "max_price"):
        if parsed.get(key) is not None and abs(float(parsed[key]) - float(value)) < 0.001:
            return True
    return False


def _grounded_ai_filters(filters: dict, evidence: dict, message: str) -> dict:
    """Оставляет фильтры модели, подтверждённые фразой из сообщения пользователя.

    Значение обязано быть каноническим (это уже проверил `_normalize_ai_filters`), а рядом
    должна стоять фраза из сообщения, которая на него указывает. Поэтому модель может
    добавлять формулировки, которых не видит регулярный парсер («тёмные» → `Black`), но не
    может опереться на служебное слово, на чужой признак («белые» для бренда) или на цифру
    из другого места сообщения.

    Подтверждение — это требование опоры на слова пользователя, а не проверка смысла: если
    модель выбрала произвольное содержательное слово из сообщения, формальная проверка
    пройдёт. Проверить соответствие значения смыслу фразы по словарю нельзя.
    """
    text = _normalize_text(message)
    fallback_keys = parse_request(message)
    grounded = {}
    dynamic: dict[str, str] = {}
    for key, value in filters.items():
        # Динамические оси каталога модель отдаёт словарём «код атрибута → значение»,
        # поэтому подтверждение проверяется для каждой пары отдельно.
        if key == "attributes" and isinstance(value, dict):
            for code, item in value.items():
                phrase = _normalize_text(evidence.get(str(code), ""))
                if _evidence_is_usable(str(code), item, phrase, text):
                    dynamic[str(code)] = str(item)
            continue
        phrase = _normalize_text(evidence.get(key, ""))
        if key == "currency":
            if fallback_keys.get("currency") == value:
                grounded[key] = value
            continue
        # Признак размера словами пользователя приходит из детерминированного разбора: если он
        # есть, размер подтверждён осью, а не только числом.
        if key == "size" and _is_a_price_number(value, fallback_keys):
            # Число в сообщении — это бюджет, а не размер: размером оно не подтверждается.
            continue
        if _evidence_is_usable(key, value, phrase, text, size_phrase="size" in fallback_keys):
            grounded[key] = value
    if dynamic:
        grounded["attributes"] = dynamic
    return grounded


def _normalize_ai_filters(filters: dict) -> dict:
    """Приводит значения модели к каноническим из справочника каталога, неизвестные отбрасывает."""
    normalized = dict(filters)
    for key, canonical in (
        ("brand", BRAND_CANONICAL),
        ("color", COLOR_CANONICAL),
        ("gender", GENDER_CANONICAL),
        ("use_case", USE_CASE_CANONICAL),
        ("feature", FEATURE_CANONICAL),
    ):
        value = normalized.get(key)
        if value is None:
            continue
        if str(value).strip().lower() in canonical:
            normalized[key] = canonical[str(value).strip().lower()]
        else:
            # Значение модели — отдельное слово, а не живая фраза, поэтому здесь принимается
            # и словарная основа («бел» → White). При разборе сообщения такая основа запрещена:
            # там она совпала бы с посторонним словом («белка», «черновик»).
            stem = MODEL_VALUE_STEMS.get(key, {}).get(str(value).strip().lower())
            if stem is not None:
                normalized[key] = stem
            else:
                normalized.pop(key, None)
    for key in _PRICE_KEYS:
        if key not in normalized:
            continue
        price = sanitize_price(normalized[key])
        if price is None:
            normalized.pop(key)
        else:
            normalized[key] = price
    if normalized.get("min_price") is not None and normalized.get("max_price") is not None and normalized["min_price"] > normalized["max_price"]:
        normalized.pop("min_price", None)
        normalized.pop("max_price", None)
    if normalized.get("currency") not in {"USD", "EUR", "UAH"}:
        normalized.pop("currency", None)
    return normalized


def _join_notes(notes: list[str], reply: str) -> str:
    """Склеивает пояснения к ответу: сначала честные оговорки, потом сам ответ."""
    return " ".join(part.strip() for part in [*notes, reply] if part and part.strip())


def safe_reply(products: list[dict], filters: dict, language: str = "ru") -> str:
    """Ответ без сумм: собирается только из количества найденного и условий запроса.

    Нужен тогда, когда обычный шаблон назвал сумму, которую не подтверждает ни один товар.
    """
    copy = COPY[language]
    count = _variant_count(len(products), language)
    summary = _filter_summary(filters, language)
    return copy["found"].format(count=count, summary=summary) if summary else copy["found_all"].format(count=count)


def _guard_response(db: Session, result: dict, filters: dict, language: str = "ru") -> dict:
    """Страховка перед отправкой: неподтверждённое до пользователя не доходит.

    Это runtime-защита, а не только проверка в тестах: каждый товар сверяется с текущим
    состоянием базы, у товара обязано быть подтверждённое пояснение, а каждая сумма в тексте —
    подтверждаться ценой товара или условием запроса. Если что-то не сходится, товар не
    показывается, а ответ пересобирается из безопасного шаблона без сумм.
    """
    logger = logging.getLogger("uvicorn.error")
    products = result.get("products") or []
    if not products:
        return result
    kept: list[dict] = []
    for product in products:
        problems = verify_products(db, [product]) + verify_reasons([product])
        if problems:
            logger.warning("grounding blocked product: %s", "; ".join(problems))
            continue
        kept.append(product)
    result["products"] = kept
    if not kept:
        result["reply"] = COPY[language]["grounding_blocked"]
        result["needs_clarification"] = True
        result["suggestions"] = COPY[language]["empty"]
        return result
    # Исключения не ослабляются никогда, поэтому их нарушение — всегда дефект.
    consistent = [product for product in kept if not verify_against_filters(product, filters, result.get("reply", ""))]
    if len(consistent) != len(kept):
        logger.warning("grounding blocked product: не отвечает названным условиям")
    kept = consistent
    result["products"] = kept
    if not kept:
        result["reply"] = COPY[language]["grounding_blocked"]
        result["needs_clarification"] = True
        result["suggestions"] = COPY[language]["empty"]
        return result
    problems = verify_reply(result.get("reply", ""), kept, filters)
    if problems:
        logger.warning("grounding blocked reply: %s", "; ".join(problems))
        result["reply"] = safe_reply(kept, filters, language)
        result["needs_clarification"] = False
        result["suggestions"] = _suggestions(True, False, language)
    return result


def _context_result(db, state, session_id, message, reply, products, language, *, clarify=False, facts_only=False, question=None, preserve_question=False):
    """Finish a contextual turn through the same fact guards and persisted history."""
    filters = state.get("filters", {})
    guard_filters = ({"size": filters["size"]} if filters.get("size") is not None else {}) if facts_only else filters
    _with_reasons(products, {} if facts_only else filters, language)
    result = _guard_response(db, {"reply": reply, "products": products,
                                "needs_clarification": clarify, "suggestions": [], "_question": question,
                                "_preserve_question": preserve_question},
                             guard_filters, language)
    if result["products"]:
        state["products"] = result["products"]
        state["last_successful_products"] = result["products"]
        state["shown_product_ids"] = list(dict.fromkeys([
            *state.get("shown_product_ids", []), *(item["id"] for item in result["products"])]))
    state["history"].append({"role": "user", "content": message})
    result = _finish(result, state, None, True, "clarification", filters, {}, {}, language)
    if facts_only:
        result["filters"] = {"size": filters["size"]} if filters.get("size") is not None else {}
    save_session_state(db, session_id, state)
    return result


def _price_choice(message):
    return bool(re.fullmatch(
        r"\s*(?:а\s*)?(?:что|какая|какие|яка|що)(?:\s+(?:из них|з них))?\s+(?:дешевле|дешевша|дешевше)\s*[?!.]*\s*",
        message, re.I))


def _pair_count(stock, language):
    tail = stock % 100
    ending = "пар"
    if not 11 <= tail <= 14:
        if stock % 10 == 1:
            ending = "пара"
        elif stock % 10 in (2, 3, 4):
            ending = "пары" if language == "ru" else "пари"
    return f"{stock} {ending}"


def _shopper_followup(db, state, session_id, message, language, display_currency):
    text = re.sub(r"[?!.]+$", "", message.strip().lower()).strip()
    fact_fields = product_fact_question(message)
    comfort_question = "comfort" in detect_absent_properties(message)
    clarification = state.setdefault("clarification", {})
    stock_question = bool(re.fullmatch(r"(?:а\s+)?(?:есть мой размер|є мій розмір)", text))
    selection = bool(re.fullmatch(r"(?:хочу|беру|выбираю|обираю)\s+(?:именно\s+|саме\s+)?(?:эту|цю)\s+пару", text))
    order_start = re.match(r"(?:(?:как|хочу) оформить (?:заказ|заявку)|как заказать|(?:як|хочу) оформити (?:замовлення|заявку)|як замовити)\b", text)
    # A contextual suffix does not turn an order question into an unrelated topic.
    order_tail = text[order_start.end():].strip() if order_start else None
    order = order_tail is not None and (not order_tail or shopping_intent(order_tail, {}))
    price_question = bool(re.fullmatch(r"(?:а\s+)?(?:сколько (?:стоит|стоят)|какая цена|яка ціна|скільки (?:коштує|коштують))", text))
    better_question = bool(re.fullmatch(r"(?:а\s+)?(?:что лучше|що краще)", text))
    thanks = text in {"спасибо", "дякую", "спасибо большое", "дуже дякую"}
    parsed = parse_request(message)
    color_followup = bool(parsed.get("color") and re.search(r"\b(?:из них|з них)\b", text)
                          and shopping_intent(message, parsed))
    stock_answer = bool(clarification.get("awaiting_stock_size") and set(parsed) == {"size"}
                        and re.fullmatch(r"(?:eu\s*)?\d+(?:[.,]\d+)?", text))
    anchors = state.get("products") or state.get("last_successful_products", [])
    pending_name = clarification.get("choosing_product") and any(
        text in {item["name"].lower(), f"{item['brand']} {item['name']}".lower()}
        for item in anchors)
    if not (stock_question or selection or order or color_followup or stock_answer or pending_name
            or price_question or fact_fields or better_question or thanks or comfort_question):
        return None
    products = []
    for anchor in anchors:
        stored = get_product(db, anchor["id"])
        if stored is not None:
            products.append(serialize_product(stored, display_currency))
    if fact_fields and clarification.get("selected_product_id"):
        selected = get_product(db, clarification["selected_product_id"])
        if selected is not None:
            products = [serialize_product(selected, display_currency)]
    filters = state["filters"]
    if stock_answer:
        filters["size"] = parsed["size"]
        state.setdefault("hard_constraints", {})["size"] = parsed["size"]
        clarification.pop("awaiting_stock_size", None)
    size = filters.get("size")
    if comfort_question:
        reply = ("Подтверждённых данных о комфорте этих моделей в каталоге нет. Могу сравнить цену, размеры и наличие."
                 if language == "ru" else
                 "Підтверджених даних про комфорт цих моделей у каталозі немає. Можу порівняти ціну, розміри та наявність.")
        return _context_result(db, state, session_id, message, reply, products, language,
                               facts_only=True, preserve_question=True)
    if thanks:
        return _context_result(db, state, session_id, message,
                               "Пожалуйста!" if language == "ru" else "Будь ласка!", [], language, preserve_question=True)
    # Repair only a generic field question persisted as a model by an older plan.
    if fact_fields and product_fact_question(filters.get("name_query") or ""):
        filters.pop("name_query", None)
        state.setdefault("hard_constraints", {}).pop("name_query", None)
    absent = missing_model_response(db, filters, language)
    if absent is not None:
        return _context_result(db, state, session_id, message, absent["reply"], [], language)
    if fact_fields and not products:
        reply = ("Какую модель кроссовок вы ищете?" if language == "ru" else
                 "Яку модель кросівок ви шукаєте?")
        return _context_result(db, state, session_id, message, reply, [], language, clarify=True, question="model")
    if better_question:
        reply = ("Что для вас важнее при выборе между этими моделями?" if language == "ru" else
                 "Що для вас важливіше при виборі між цими моделями?") if len(products) >= 2 else (
                 "Какие модели вы хотите сравнить?" if language == "ru" else "Які моделі ви хочете порівняти?")
        return _context_result(db, state, session_id, message, reply, products, language, clarify=True, facts_only=True, question="criterion")
    if price_question or (fact_fields and not stock_question):
        if not products:
            reply = "Цену какой модели проверить?" if language == "ru" else "Ціну якої моделі перевірити?"
        else:
            lines = []
            for product in products:
                amount, currency = _price_for_size(product, size)
                variant = next((item for item in product["variants"] if item.get("size") == size), None)
                suffix = f", EU {size:g}" if variant is not None else ""
                if size is not None and (variant is None or variant["stock"] <= 0):
                    suffix += "; " + ("" if variant is not None else f"EU {size:g} ") + (
                        "нет в наличии" if language == "ru" else "немає в наявності")
                fields = fact_fields or {"price"}
                label = f"{product['brand']} {product['name']}"
                if "price" in fields:
                    label += f" — {amount:g} {currency}{suffix}"
                lines.append(label + ".")
                if "size" in fields:
                    sizes = ", ".join(f"EU {value:g}" for value in product["available_sizes"])
                    lines.append(("Доступные размеры: " if language == "ru" else "Доступні розміри: ") +
                                 (sizes or ("нет в наличии" if language == "ru" else "немає в наявності")) + ".")
                if "availability" in fields:
                    stock = variant["stock"] if variant else (0 if size is not None else product["total_stock"])
                    focus = f"EU {size:g}: " if size is not None else ""
                    lines.append(focus + ("В наличии " if language == "ru" else "У наявності ") +
                                 _pair_count(stock, language) + ".")
            reply = " ".join(lines)
        return _context_result(db, state, session_id, message, reply, products, language, clarify=not products,
                               facts_only=True, question="model" if not products else None)
    if color_followup:
        filters["color"] = parsed["color"]
        state.setdefault("hard_constraints", {})["color"] = parsed["color"]
        allowed = {item["id"] for item in search_products(db, **{**filters, "display_currency": display_currency})}
        matching = [item for item in products if item["id"] in allowed]
        state["products"] = matching
        if matching:
            reply = ("Из показанных подходят: " if language == "ru" else "Із показаних підходять: ") + "; ".join(
                f"{item['brand']} {item['name']}" for item in matching) + "."
        else:
            reply = ((f"Среди показанных моделей таких нет. Условия «{_filter_summary(filters, language)}» сохранены. Какой цвет проверить?"
                      if language == "ru" else
                      f"Серед показаних моделей таких немає. Умови «{_filter_summary(filters, language)}» збережено. Який колір перевірити?"))
        return _context_result(db, state, session_id, message, reply, matching, language, clarify=not matching,
                               question="color" if not matching else None)
    if stock_question or stock_answer:
        if size is None:
            clarification["awaiting_stock_size"] = True
            reply = "Какой EU-размер проверить?" if language == "ru" else "Який EU-розмір перевірити?"
            return _context_result(db, state, session_id, message, reply, [], language, clarify=True, question="eu_size")
        if products:
            lines = []
            for product in products:
                variant = next((item for item in product["variants"] if item["size"] == size), None)
                stock = variant["stock"] if variant else 0
                status = ((f"в наличии {_pair_count(stock, language)}" if language == "ru" else f"у наявності {_pair_count(stock, language)}")
                          if stock > 0 else ("нет в наличии" if language == "ru" else "немає в наявності"))
                lines.append(f"{product['brand']} {product['name']}, EU {size:g}: {status}.")
            return _context_result(db, state, session_id, message, " ".join(lines), products, language, facts_only=True)
    if selection or order or pending_name:
        allowed = {item["id"] for item in search_products(db, **{**filters, "display_currency": display_currency})}
        candidates = [item for item in products if item["id"] in allowed]
        selected = next((item for item in candidates if item["id"] == clarification.get("selected_product_id")), None)
        if pending_name:
            selected = next((item for item in candidates if text in {
                item["name"].lower(), f"{item['brand']} {item['name']}".lower()}), None)
        if selected is None and len(products) == 1 and len(candidates) == 1:
            selected = candidates[0]
        if selected is not None:
            clarification["selected_product_id"] = selected["id"]
            clarification.pop("choosing_product", None)
            filters["name_query"] = selected["name"]
            if size is None:
                clarification["awaiting_stock_size"] = True
                reply = (f"Вы выбрали {selected['brand']} {selected['name']}. Какой EU-размер проверить?" if language == "ru" else
                         f"Ви обрали {selected['brand']} {selected['name']}. Який EU-розмір перевірити?")
                return _context_result(db, state, session_id, message, reply, [selected], language, clarify=True, question="eu_size")
            reply = (f"{selected['brand']} {selected['name']}, EU {size:g}. Откройте карточку, выберите этот размер и нажмите «Оставить заявку». Укажите имя и контакт — это заявка на связь, без оплаты."
                     if language == "ru" else
                     f"{selected['brand']} {selected['name']}, EU {size:g}. Відкрийте картку, оберіть цей розмір і натисніть «Залишити заявку». Вкажіть ім’я та контакт — це заявка для зв’язку, без оплати.")
            if not order:
                reply = (f"Вы выбрали {selected['brand']} {selected['name']}, EU {size:g}. Заявку можно оставить в карточке этой модели."
                         if language == "ru" else
                         f"Ви обрали {selected['brand']} {selected['name']}, EU {size:g}. Заявку можна залишити в картці цієї моделі.")
            return _context_result(db, state, session_id, message, reply, [selected], language)
        if candidates:
            clarification["choosing_product"] = True
            names = "; ".join(f"{item['brand']} {item['name']}" for item in candidates)
            reply = (f"Какую пару вы выбрали: {names}?" if language == "ru" else f"Яку пару ви обрали: {names}?")
        elif products:
            reply = ("Эти модели не подходят под текущие условия. Изменим цвет, размер или бюджет?" if language == "ru" else
                     "Ці моделі не відповідають поточним умовам. Змінимо колір, розмір чи бюджет?")
        else:
            reply = ("Сначала выберите модель в каталоге или опишите, какую пару ищете." if language == "ru" else
                     "Спочатку оберіть модель у каталозі або опишіть, яку пару шукаєте.")
        return _context_result(db, state, session_id, message, reply, [], language, clarify=True,
                               question="model" if candidates else "condition" if products else None)
    return None


def chat(db: Session, session_id: str, message: str, language: str = "ru", display_currency: str = "USD") -> dict:
    """Opt-in TurnPlan path, with the unchanged rule engine as the failure path."""
    from copy import deepcopy
    from ..ai.groq_provider import get_turn_plan_provider, public_context
    from ..ai.turn_plan import validate_plan_context
    from ..ai.turn_execution import execute_turn_plan

    provider = get_turn_plan_provider()
    # SKU lookup already has an exact deterministic contract; never reinterpret identifiers.
    if (provider is None or _is_greeting(message) or parse_request(message).get("sku_query")
            or product_fact_question(message) or selection_help_request(message)):
        return _fallback_chat(db, session_id, message, language, display_currency)
    language = "uk" if language == "uk" else "ru"
    original = _sessions.get(session_id)
    if original is None:
        original = load_session_state(db, session_id)
    state = deepcopy(original)
    try:
        plan = provider.plan(state, message, language, display_currency)
        validate_plan_context(plan, message, public_context(original, language, display_currency))
        result = execute_turn_plan(db, state, plan, message, language, display_currency)
    except (ProviderUnavailable, ValueError):
        # No session changes have been committed; retry the same turn with the rule engine.
        return _fallback_chat(db, session_id, message, language, display_currency)
    facts_only = result.pop("_facts_only", False)
    filters = state["filters"]
    guard_filters = ({"size": filters["size"]} if filters.get("size") is not None else {}) if facts_only else filters
    _with_reasons(result["products"], guard_filters, language)
    result = _guard_response(db, result, guard_filters, language)
    state["products"] = result["products"]
    if result["products"]:
        state["last_successful_products"] = result["products"]
        state["shown_product_ids"] = list(dict.fromkeys([
            *state.get("shown_product_ids", []), *(item["id"] for item in result["products"])]))
    state.setdefault("history", []).append({"role": "user", "content": message})
    result = _finish(result, state, None, False, plan.intent, filters, {}, {}, language)
    result["mode"] = "groq"
    if facts_only:
        result["filters"] = {"size": filters["size"]} if filters.get("size") is not None else {}
    save_session_state(db, session_id, state)
    _sessions[session_id] = state
    return result


def _fallback_chat(db: Session, session_id: str, message: str, language: str = "ru", display_currency: str = "USD") -> dict:
    language = "uk" if language == "uk" else "ru"
    copy = COPY[language]
    state = _sessions.get(session_id)
    if state is None:
        state = load_session_state(db, session_id)
        _sessions[session_id] = state
    state.setdefault("preferences", {})
    state.setdefault("hard_constraints", {})
    state.setdefault("last_successful_products", [])
    if _is_greeting(message):
        return _context_result(db, state, session_id, message, copy["greeting"], [], language,
                               preserve_question=True)
    initial_filters = parse_request(message)
    if not initial_filters.get("sku_query"):
        initial_filters.update(catalog_model_filters(db, message))
    normalized, immediate, contextual_answer = prepare_turn(state, message, initial_filters, language)
    if immediate is not None:
        return _context_result(db, state, session_id, message, immediate["reply"], [], language,
                               clarify=immediate["needs_clarification"], question=immediate.get("_question"))
    message = normalized
    contextual = _shopper_followup(db, state, session_id, message, language, display_currency)
    if contextual is not None:
        return contextual
    exact_sku_request = bool(parse_request(message).get("sku_query"))
    comparison_names = [] if exact_sku_request else comparison_model_names(db, message)
    reference_comparison = bool(re.fullmatch(
        r"\s*(?:сравни\w*\s+эти|порівняй\s+ці)\s+(?:(?:две|дві)\s+)?(?:модели|пары|моделі|пари)\s*[?!.]*\s*", message, re.I))
    anchors = state.get("products") or state.get("last_successful_products", [])
    if reference_comparison and len(anchors) != 2:
        names = "; ".join(f"{item['brand']} {item['name']}" for item in anchors)
        reply = ((f"Какие две модели сравнить: {names}?" if names else "Назовите две модели для сравнения.")
                 if language == "ru" else
                 (f"Які дві моделі порівняти: {names}?" if names else "Назвіть дві моделі для порівняння."))
        return _context_result(db, state, session_id, message, reply, [], language, clarify=True, question="comparison_models")
    if not comparison_names and (reference_comparison or (_price_choice(message) and len(anchors) >= 2)):
        comparison_names = [item["name"] for item in anchors]
    comparison_state = state.get("comparison") or {}
    comparison_followup = bool(
        comparison_state.get("models")
        and not exact_sku_request
        and (_price_choice(message) or re.fullmatch(
            r"\s*(?:а\s*)?(?:какая\s+дешевле|какие\s+размеры(?:\s+есть)?|"
            r"яка\s+дешевша|які\s+розміри(?:\s+є)?|which\s+is\s+cheaper|what\s+sizes(?:\s+are\s+available)?)\s*[?!.]*\s*",
            message,
            re.I,
        ))
    )
    if comparison_state and not comparison_names and not comparison_followup:
        state["comparison"] = None
    if comparison_names or comparison_followup:
        names = comparison_names or comparison_state["models"]
        products = _comparison_products(db, names, display_currency)
        state["comparison"] = {"models": names}
        state["products"] = products
        state["shown_product_ids"] = list(dict.fromkeys(
            [*state.get("shown_product_ids", []), *(item["id"] for item in products)]
        ))
        reply = _comparison_reply(names, products, message, language, state.get("filters", {}).get("size"),
                                  compact=True)
        return _context_result(db, state, session_id, message, reply, products, language, facts_only=True)
    size_followup = re.fullmatch(
        r"\s*(?:а\s*)?(?:какие\s+размеры(?:\s+есть)?|які\s+розміри(?:\s+є)?|what\s+sizes(?:\s+are\s+available)?)\s*[?!.]*\s*",
        message,
        re.I,
    )
    if size_followup and state.get("products"):
        products = []
        for previous in state["products"]:
            product = get_product(db, int(previous["id"]))
            if product is not None:
                products.append(serialize_product(product, display_currency))
        state["products"] = products
        names_and_sizes = []
        for product in products:
            sizes = ", ".join(f"EU {size:g}" for size in product.get("available_sizes", []))
            size_text = sizes or ("нет размеров в наличии" if language == "ru" else "немає розмірів у наявності")
            names_and_sizes.append(f"{product['brand']} {product['name']}: {size_text}")
        if products:
            reply = ("Размеры в наличии по каталогу: " if language == "ru" else
                     "Розміри в наявності за каталогом: ") + "; ".join(names_and_sizes) + "."
            return _context_result(db, state, session_id, message, reply, products, language, facts_only=True)
    lowered = message.lower().replace(",", ".")
    parsed_message = parse_request(message)
    sku_query = parsed_message.get("sku_query")
    if isinstance(sku_query, str):
        # SKU digits are identifiers, not shoe sizes (e.g. the 999 in an unknown SKU).
        lowered = lowered.replace(sku_query, " " * len(sku_query))
    more_requested = _is_more_request(lowered)
    similar_requested = is_similar_request(lowered)
    recommendation_requested = is_recommendation_request(lowered)
    cheaper_requested = "дешевл" in lowered or "дешевш" in lowered
    removals = detect_removals(lowered)
    absent_properties = detect_absent_properties(lowered)
    # Отрицания и невозможные значения разбираются детерминированно: модель их не предлагает.
    negations = detect_negations(lowered)
    unknown_values = detect_unknown_catalogue_values(lowered)
    out_of_range_size = detect_out_of_range_size(lowered)
    notes: list[str] = []

    if not sku_query:
        parsed_message.update(catalog_model_filters(db, message))
    measurement = update_measurement(state, message, parsed_message.get("size"))
    # Доменный gate: без товарного контекста сообщение не разбирается как запрос каталога,
    # иначе «пума на дереві» или «я работаю в офисе» стали бы фильтрами.
    shopping = (bool(sku_query) or contextual_answer
                or shopping_intent(message, parsed_message, established=state.get("shopping", False)))
    if parsed_message.get("name_query"):
        model_text = re.sub(r"\b(?:fresh\s+foam(?:\s+x)?\s+)?1080(?:\s*v\d+)?\b", " ", lowered)
        model_text = re.sub(r"(?<!\w)" + re.escape(parsed_message["name_query"].lower()) + r"(?!\w)", " ", model_text)
        shopping = shopping or not model_text.strip() or shopping_intent(model_text, parsed_message)
    if measurement and (
            re.search(r"\d\s*(?:см|cm|jp)\b|\b(?:cm|jp)\s*\d", lowered)
            or re.search(r"устіл\w*|стель\w*|стоп\w*|\b(?:foot|insole)\b", lowered)):
        shopping = True
    if absent_properties:
        shopping = True
    if shopping:
        state["shopping"] = True
    else:
        # Сообщение не про товар: из него не берётся ни условие, ни снятие, ни сброс — и это
        # верно и внутри открытого сеанса. Иначе «покажи все новости за неделю» снимало бы
        # условия, «любой размеренный темп» — размер, а «пума на дереві» меняла бы бренд.
        removals = set()
        negations = {}
    fallback_filters = parsed_message if shopping else {}
    preference_updates = _soft_preference_updates(message, fallback_filters, display_currency) if shopping else {}
    # Если значение оси названо явно, снятием это быть не может: «любой бренд, но размер 43»
    # снимает бренд, а размер остаётся.
    named_axes = {"brand": "brand", "color": "color", "size": "size", "price": "price"}
    for axis, key in named_axes.items():
        if axis not in removals:
            continue
        if key == "price":
            named = fallback_filters.get("min_price") is not None or fallback_filters.get("max_price") is not None
        else:
            named = fallback_filters.get(key) is not None
        if named:
            removals.discard(axis)
    extracted = fallback_filters
    provider_failed = True
    intent = "search_products"
    ai_filters: dict[str, str | float] = {}
    ai_rejections: list[str] = []
    try:
        # Настройки читаются тоже под защитой: опечатка в переменной окружения не должна
        # превращаться в ошибку запроса пользователя.
        provider = get_provider()
    except Exception as error:  # noqa: BLE001 — провайдер не должен ломать запрос
        provider, provider_failed = None, True
        logging.getLogger("uvicorn.error").warning("provider setup failed: %s: %s", type(error).__name__, error)
    if provider:
        try:
            raw = provider.analyze(state["history"], message)
            # Ворота смотрят на сырой ответ модели, до приведения типов: иначе pydantic молча
            # превратил бы «до 120» в отсутствующее значение, и отказ остался бы незамеченным.
            validated = validate_analysis(db, raw)
            ai_rejections = validated.rejected
            if validated.contract_broken:
                raise ProviderUnavailable(f"contract violation: {', '.join(ai_rejections)}")
            # После ворот значение проходит проверку типов и словарную канонизацию, а затем
            # подтверждение признаком из сообщения пользователя.
            analysis = AIAnalysis.model_validate({**raw, "intent": validated.intent, "filters": validated.filters})
            ai_filters = _grounded_ai_filters(
                _normalize_ai_filters({key: value for key, value in analysis.filters.model_dump().items() if value is not None}),
                analysis.evidence,
                message,
            )
            extracted = {**ai_filters, **fallback_filters} if shopping else {}
            for key in preference_updates:
                if key == "brand":
                    extracted.pop("brand", None)
                elif key == "price_target":
                    extracted.pop("min_price", None)
                    extracted.pop("max_price", None)
                    extracted.pop("currency", None)
            intent, provider_failed = validated.intent, False
        except (ProviderUnavailable, ValueError) as error:
            # Сбой провайдера — не ошибка пользователя: работает детерминированный разбор.
            logging.getLogger("uvicorn.error").info("provider unavailable: %s: %s", type(error).__name__, error)
        except Exception as error:  # noqa: BLE001 — любой сбой модели ведёт к safe fallback
            logging.getLogger("uvicorn.error").warning("provider failed: %s: %s", type(error).__name__, error)
    if ai_rejections or os.getenv("AI_DEBUG", "").lower() in {"1", "true"}:
        logging.getLogger("uvicorn.error").info(
            "ai validation: intent=%s accepted=%s rejected=%s", intent, ai_filters, ai_rejections
        )

    # Условия прошлых ходов сохраняются: смена одного параметра не начинает поиск заново.
    previous = dict(state["filters"])
    hard_constraints = dict(state.get("hard_constraints") or {})
    if (more_requested and re.search(r"другие|інші", lowered)
            and "name_query" not in fallback_filters and not sku_query):
        selected_id = state["clarification"].pop("selected_product_id", None)
        if selected_id is not None or previous.get("name_query"):
            state["shown_product_ids"] = ([selected_id] if selected_id is not None else
                                          [item["id"] for item in state.get("products", [])])
        previous.pop("name_query", None)
        previous.pop("sku_query", None)
        hard_constraints.pop("name_query", None)
    preferences = dict(state.get("preferences") or {})
    preferences.update(preference_updates)
    if "brand" in preference_updates:
        previous.pop("brand", None)
        previous.pop("name_query", None)
        hard_constraints.pop("brand", None)
    if "price_target" in preference_updates:
        previous.pop("min_price", None)
        previous.pop("max_price", None)
        previous.pop("currency", None)
        hard_constraints.pop("min_price", None)
        hard_constraints.pop("max_price", None)
        hard_constraints.pop("currency", None)
    if fallback_filters.get("brand") and "brand" not in preference_updates:
        preferences.pop("brand", None)
        if previous.get("brand") != fallback_filters["brand"]:
            hard_constraints.pop("brand", None)
    if cheaper_requested and "name_query" not in fallback_filters and not sku_query:
        # An explicit request for a cheaper alternative releases the previously shown model
        # as a product lock, while retaining its brand, purpose, size and budget constraints.
        previous.pop("name_query", None)
        hard_constraints.pop("name_query", None)
        if "size" not in fallback_filters:
            state["skip_size"] = True
    if sku_query:
        # An exact identifier lookup is self-contained and must not inherit prior filters.
        previous = {}
        hard_constraints = {}
        state["browse"] = False
    if measurement and "size" not in fallback_filters:
        # A model must not turn a centimetre measurement into an invented EU size.
        extracted.pop("size", None)
        if isinstance(extracted.get("attributes"), dict):
            extracted["attributes"].pop("size", None)
    # A newly named brand starts a different model search, while size/budget persist.
    if (fallback_filters.get("brand") and fallback_filters.get("brand") != previous.get("brand")
            and "name_query" not in fallback_filters):
        previous.pop("name_query", None)
    if "brand" in removals:
        previous.pop("name_query", None)
    if extracted.get("use_case") and extracted.get("use_case") != previous.get("use_case") and "feature" not in extracted:
        previous.pop("feature", None)
    if extracted.get("feature") and extracted.get("feature") != previous.get("feature") and "use_case" not in extracted:
        previous.pop("use_case", None)
    # Размер и бюджет, названные пользователем раньше, сильнее значений от модели: модель
    # не подменяет уже заявленное молча.
    for key in _TURN_SCOPED_KEYS:
        if key not in fallback_filters and key in previous:
            extracted.pop(key, None)
    # Значение, не подтверждённое признаком в тексте текущего сообщения, действует только в этом ходе.
    turn_scoped_keys = {key for key in _TURN_SCOPED_KEYS if key in extracted and key not in fallback_filters}
    if sku_query:
        turn_scoped_keys.add("sku_query")
    filters = {**previous, **extracted}
    for key in ("size", "min_price", "max_price"):
        if key in fallback_filters:
            hard_constraints[key] = fallback_filters[key]
    if fallback_filters.get("brand") and "brand" not in preference_updates:
        hard_constraints["brand"] = fallback_filters["brand"]
    if fallback_filters.get("color"):
        hard_constraints["color"] = fallback_filters["color"]
    if fallback_filters.get("name_query"):
        hard_constraints["name_query"] = fallback_filters["name_query"]
    if ((filters.get("min_price") is not None or filters.get("max_price") is not None)
            and not filters.get("currency")):
        filters["currency"] = display_currency
    if extracted:
        # Пользователь назвал условие — режим просмотра каталога закончился.
        state["browse"] = False
    if extracted.get("size") is not None:
        # Пользователь снова назвал размер — вопрос о размере снимается.
        state["skip_size"] = False

    # Снятие условий фразами: «бренд не важен», «любой цвет», «покажи все».
    removed_all = "all" in removals
    if removed_all:
        # Просмотр всего каталога: продолжаем его и на следующих ходах, пока пользователь
        # не назовёт новое условие.
        filters = {}
        hard_constraints = {}
        preferences = {}
        state["skip_size"] = True
        state["browse"] = True
    else:
        for key in ("brand", "color", "size", "price"):
            if key in removals:
                if key == "price":
                    filters.pop("min_price", None)
                    filters.pop("max_price", None)
                    hard_constraints.pop("min_price", None)
                    hard_constraints.pop("max_price", None)
                    preferences.pop("price_target", None)
                else:
                    filters.pop(key, None)
                    hard_constraints.pop(key, None)
                    if key == "brand":
                        preferences.pop("brand", None)
                if key == "size":
                    notes.append(copy["size_dropped"])
                    # Условие снято осознанно: больше о размере не спрашиваем.
                    state["skip_size"] = True
    skip_size_question = state.get("skip_size", False)
    if filters.get("name_query") or filters.get("sku_query"):
        # Конкретную модель сначала показываем с доступными размерами: покупатель спросил
        # наличие/вариант, а не просил пройти анкету подбора.
        skip_size_question = True

    # Отрицания: «не Nike», «только не белые», «не 43». Значение, названное и тут же
    # исключённое, не учитывается вовсе — и об этом говорится, а не угадывается молча.
    # Conflicting positive and negative conditions are retained and clarified below.
    resolving_conflict = bool(state.get("clarification", {}).get("conflict_question"))
    if resolving_conflict:
        for axis, values in negations.items():
            if axis not in fallback_filters:
                for value in values:
                    _drop_included_value(filters, axis, value)
    # Снятое условие перестаёт действовать и как исключение: «цвет не важен» отменяет «не белые».
    # Исключения прошлых ходов переносятся, но только по тем осям, которые не сняты.
    removed_axes = set(removals)
    # «Покажи все» снимает всё, включая исключения: пользователь просил показать каталог целиком.
    carried_attributes = {} if removed_all else {
        code: values
        for code, values in (previous.get("exclude_attributes") or {}).items()
        if _AXIS_BY_ATTRIBUTE_CODE.get(str(code), str(code)) not in removed_axes
    }
    carried_brand = [] if removed_all or "brand" in removed_axes else list(previous.get("exclude_brand") or [])
    if resolving_conflict:
        if fallback_filters.get("brand") and not negations.get("brand"):
            carried_brand = [value for value in carried_brand if value != fallback_filters["brand"]]
        for code in list(carried_attributes):
            axis = _AXIS_BY_ATTRIBUTE_CODE.get(code, code)
            if axis in fallback_filters and not negations.get(axis):
                value = fallback_filters[axis]
                values = value if isinstance(value, list) else [value]
                carried_attributes[code] = [item for item in carried_attributes[code] if item not in values]
                if not carried_attributes[code]:
                    carried_attributes.pop(code)
    if removed_all:
        negations = {}
    # Новые исключения не сбрасываются снятием оси: «любой размер, но не 43» — это исключение,
    # а перенесённые исключения снятой оси отсекаются выше через carried_attributes.
    new_brand, new_attributes = _exclusion_conditions(negations)
    merged_brand = list(carried_brand)
    for item in new_brand:
        if item not in merged_brand:
            merged_brand.append(item)
    merged_attributes = _merge_exclusions(carried_attributes, new_attributes)
    # Пустое исключение убирается из условий: иначе снятое условие осталось бы в силе.
    if merged_brand:
        filters["exclude_brand"] = merged_brand
    else:
        filters.pop("exclude_brand", None)
    if merged_attributes:
        filters["exclude_attributes"] = merged_attributes
    else:
        filters.pop("exclude_attributes", None)

    # Все ветки, меняющие состояние и режимы, работают только в товарном контексте:
    # нетоварная фраза не сбрасывает условия, не включает «похожие» и не считает «дешевле».
    if shopping and any(phrase in lowered for phrase in NEW_SEARCH_PHRASES):
        settings = dict(extracted)
        filters = settings
        hard_constraints = {}
        preferences = dict(preference_updates)
        for key in ("size", "min_price", "max_price", "name_query"):
            if key in settings:
                hard_constraints[key] = settings[key]
        if settings.get("brand") and "brand" not in preferences:
            hard_constraints["brand"] = settings["brand"]
        if settings.get("color"):
            hard_constraints["color"] = settings["color"]
        state["skip_size"] = False
        state["browse"] = False
    if "air max" in lowered:
        if shopping and not fallback_filters.get("name_query"):
            filters["name_query"] = "air max"
    cheaper_reference = None
    cheaper_filters = None
    if shopping and cheaper_requested:
        cheaper_anchors = state["products"] or state.get("last_successful_products", [])
        previous_currency = cheaper_anchors[0].get("currency", "USD") if cheaper_anchors else None
        previous_prices = [
            float(product["price"]) for product in cheaper_anchors
            if product.get("currency", "USD") == previous_currency
        ]
        cheaper_filters = dict(filters)
        if previous_prices:
            cheaper_filters["currency"] = previous_currency
            cheaper_reference = min(previous_prices)
            cheaper_filters["max_price"] = min(
                float(filters["max_price"]) if filters.get("max_price") is not None else cheaper_reference - 1,
                cheaper_reference - 1,
            )
        else:
            preferences["price_order"] = "low"
        # A relative comparison is a one-turn query, not a new shopper budget.
    # Бюджет не должен остаться противоречивым после смены ограничения: старое значение уходит,
    # новое (сказанное в этом сообщении) остаётся активным.
    # Never silently discard a budget boundary; ask which condition to change.

    # Новая самостоятельная выдача не должна случайно ссылаться на старый результат.
    if removed_all or (shopping and any(phrase in lowered for phrase in NEW_SEARCH_PHRASES)):
        state["last_successful_products"] = []
        state["last_successful_product_ids"] = []
    # Уточнение без совпадений не должно стирать предыдущую успешную выдачу:
    # «похожие» всё ещё может опираться на последнюю показанную модель.
    anchor_products = state["products"] or state.get("last_successful_products", [])
    anchor = anchor_products[0] if anchor_products else None
    similar_anchor_text = None
    similar_overlay = None
    if shopping and similar_requested:
        if anchor is not None:
            # «Похожие» получают временный overlay и не переписывают условия пользователя:
            # иначе вычисленные системой признаки остались бы в состоянии диалога.
            similar_overlay = _similar_filters(anchor, filters, language)
            state["similar_overlay"] = similar_overlay
            similar_anchor_text = f"{anchor['brand']} {anchor['name']}"
        else:
            notes.append(copy["similar_none"])
        skip_size_question = True
    elif shopping and more_requested and state.get("similar_overlay") and not removed_all:
        # Продолжение может опираться на тот же overlay, но состояние пользователя не меняется.
        similar_overlay = state["similar_overlay"]
    elif shopping:
        state["similar_overlay"] = None
    # Условия этого поиска. Overlay «похожих» действует только на один поиск и не становится
    # состоянием диалога: постоянный источник состояния — только условия пользователя.
    search_filters = similar_overlay or cheaper_filters or filters

    filters_changed = filters != previous or bool(preference_updates)
    if shopping and (filters_changed or not more_requested):
        state["shown_product_ids"] = []
    if similar_anchor_text and anchor is not None:
        # «Похожие» — это ссылка на предыдущую выдачу: уже показанное не повторяем.
        state["shown_product_ids"] = [product["id"] for product in state["products"]]
    state["history"].append({"role": "user", "content": message})
    state["filters"] = _context_filters(filters, turn_scoped_keys)
    state["preferences"] = preferences
    state["hard_constraints"] = hard_constraints

    if shopping and absent_properties:
        labels = ", ".join(ABSENT_PROPERTY_LABELS[language][code] for code in absent_properties)
        notes.append(copy["absent"].format(label=labels))

    # Значение, которого в каталоге нет, не превращается в фильтр и не исчезает молча:
    # ассистент называет его и объясняет, по чему подбирает дальше.
    for axis, value in unknown_values:
        if axis == "model":
            label = UNKNOWN_CATALOGUE_LABELS[language][axis].format(value=value)
            notes.append(copy["unknown_model"].format(label=label))
            skip_size_question = True
        elif axis == "brand":
            available = ", ".join(sorted(catalogue_brands(db)))
            notes.append(copy["unknown_brand"].format(value=value, available=available))
        else:
            label = UNKNOWN_CATALOGUE_LABELS[language][axis].format(value=value)
            notes.append(copy["unknown_value"].format(label=label))

    browse_mode = removed_all or state.get("browse", False)
    is_greeting = _is_greeting(message)
    safety_result = None
    if shopping:
        safety_result = conflict_response(state, filters, language)
        if safety_result is None:
            safety_result = missing_model_response(db, filters, language)
        if safety_result is None:
            safety_result = measurement_response(db, state, filters, language, display_currency, more_requested)
    if safety_result is not None:
        result = safety_result
        state["products"] = result["products"]
        state["shown_product_ids"].extend(
            product["id"] for product in result["products"] if product["id"] not in state["shown_product_ids"]
        )
    elif shopping and "fit" in absent_properties and filters.get("name_query"):
        product_name = f"{filters.get('brand', '')} {filters['name_query']}".strip()
        result = {
            "reply": copy["ask_fit_followup"].format(product=product_name),
            "products": [], "needs_clarification": True, "suggestions": [], "_question": "confirm_show_model",
        }
    elif out_of_range_size is not None and not browse_mode:
        # Размера не существует: и применить его нельзя, и молча искать другое — значит
        # ответить на другой вопрос. Поэтому спрашиваем размер, называя границы каталога.
        minimum, maximum = size_axis_bounds()
        result = {
            "reply": _join_notes(notes, copy["size_out_of_range"].format(size=out_of_range_size, minimum=minimum, maximum=maximum)),
            "products": [],
            "needs_clarification": True,
            "suggestions": _suggestions(False, True, language), "_question": "eu_size",
        }
    elif is_greeting:
        # Приветствие всегда получает ответ-приветствие; режим просмотра меняется только
        # в товарном контексте, иначе нетоварная фраза молча выключала бы каталог.
        if shopping:
            state["browse"] = False
        result = {"reply": _join_notes(notes, copy["greeting"]), "products": [], "needs_clarification": True,
                  "suggestions": [], "_preserve_question": True}
    elif not shopping:
        # An interruption suspends the task, rather than starting another sales flow.
        state["dialogue"]["transition"] = "suspend"
        model = filters.get("name_query")
        reply = ("Я можу допомогти з каталогом кросівок." if language == "uk" else
                 "Я могу помочь с каталогом кроссовок.")
        if model:
            reply += (f" Модель «{model}» і ваші умови збережено." if language == "uk" else
                      f" Модель «{model}» и ваши условия сохранены.")
        result = {"reply": reply, "products": [], "needs_clarification": False,
                  "suggestions": [], "_preserve_question": True}
    elif not filters and not preferences and not browse_mode:
        ask = copy["ask_catalog_attribute"] if absent_properties else discovery_question(state, message, language)
        if ask:
            result = {"reply": _join_notes(notes, ask), "products": [], "needs_clarification": True,
                      "suggestions": [], "_question": "need"}
        else:
            result = _search_with_fallbacks(db, state, search_filters, lowercased=lowered, cheaper=cheaper_requested,
                                            language=language, notes=notes, similar_anchor=similar_anchor_text,
                                            display_currency=display_currency)
    elif ("size" not in filters and not skip_size_question and not browse_mode and not preferences
          and not any(key in filters for key in ("min_price", "max_price"))
          and (state["dialogue"].get("last_question") or {}).get("kind") != "eu_size"):
        # Один вопрос вместо анкеты: остальное уже известно или не критично.
        purpose = USE_CASE_LABELS[language].get(str(filters.get("use_case")), "")
        reply = copy["ask_size_with_purpose"].format(purpose=purpose) if purpose else copy["ask_size"]
        result = {"reply": _join_notes(notes, reply), "products": [], "needs_clarification": True, "_question": "eu_size"}
    else:
        result = _search_with_fallbacks(db, state, search_filters, lowercased=lowered, cheaper=cheaper_requested,
                                        shopping=shopping, cheaper_reference=cheaper_reference,
                                        preferences=preferences, hard_constraints=hard_constraints,
                                        language=language, notes=notes, similar_anchor=similar_anchor_text,
                                        display_currency=display_currency)
    if shopping and measurement and measurement.get("value") is not None and state.get("clarification", {}).get("explicit_eu") is not None:
        note = ("Шукаю за вказаним EU-розміром; відповідність сантиметрам не підтверджена."
                if language == "uk" else "Ищу по указанному EU-размеру; соответствие сантиметрам не подтверждено.")
        result["reply"] = note + " " + result["reply"]
    price_target = preferences.get("price_target") or {}
    if result.get("products") and isinstance(price_target, dict):
        target_currency = str(price_target.get("currency") or "")
        known_currencies = {str(item.get("base_currency") or item.get("currency") or "")
                            for item in result["products"]}
        if target_currency and target_currency not in known_currencies:
            result["reply"] = copy["price_target_currency_unavailable"].format(
                amount=float(price_target.get("amount", 0)), currency=target_currency
            ) + result["reply"]
    # Страховка на выходе: всё, что утверждается о товаре, обязано быть подтверждено базой.
    # Страховка проверяет условия, по которым действительно шёл поиск.
    result = _guard_response(db, result, search_filters, language)
    # Bind an explicit named choice only after normal merging, stock search and grounding.
    # This also handles a model named together with a new size or budget.
    if (shopping and parsed_message.get("name_query") and not result["needs_clarification"]
            and re.match(r"\s*(?:выбираю|обираю|беру)\b", message, re.I)
            and not negations and len(result["products"]) == 1):
        chosen = result["products"][0]
        if chosen["name"] == filters.get("name_query"):
            state["clarification"]["selected_product_id"] = chosen["id"]
            if filters.get("size") is not None:
                label = f"{chosen['brand']} {chosen['name']}, EU {filters['size']:g}"
                result["reply"] = (f"Вы выбрали {label}. Заявку можно оставить в карточке этой модели."
                                   if language == "ru" else
                                   f"Ви обрали {label}. Заявку можна залишити в картці цієї моделі.")
    # Модель может выбрать только точные варианты, собранные приложением из проверенного ответа.
    # Произвольный текст от Ollama никогда не попадает покупателю.
    if provider and not provider_failed and hasattr(provider, "respond"):
        base_reply = result["reply"]
        candidates = [base_reply]
        if result.get("products"):
            candidates.extend((f"Подобрал варианты. {base_reply}", f"Вот варианты из каталога. {base_reply}"))
        try:
            generated = provider.respond(candidates)
            if generated in candidates:
                result["reply"] = generated
                result["_ai_reply_used"] = generated != base_reply
        except Exception as error:  # noqa: BLE001 — недоступность ответа не ломает поиск
            logging.getLogger("uvicorn.error").info("provider reply unavailable: %s", type(error).__name__)
    result = _guard_response(db, result, search_filters, language)
    # В состояние диалога уходят только условия пользователя.
    result = _finish(result, state, provider, provider_failed, intent, filters, fallback_filters, ai_filters, language)
    save_session_state(db, session_id, state)
    return result


def _comparison_products(db, names, display_currency):
    """Use the existing catalog search once per requested model, without relaxed matches."""
    found = []
    for name in names:
        matches = recommend_products(db, name_query=name, display_currency=display_currency, limit=10)
        exact = next((item for item in matches if item["name"].casefold() == name.casefold()), None)
        if exact is not None:
            found.append(exact)
    return found


def _price_for_size(product, size):
    variant = next((item for item in product.get("variants", []) if item.get("size") == size), None)
    if variant is None:
        return product["price"], product["currency"]
    source = product.get("base_currency") or product["currency"]
    target = product["currency"]
    amount = convert_amount(variant["price"], source, target,
                            get_rates().get("rates") if source != target else None)
    return (amount, target) if amount is not None else (variant["price"], source)


def _comparison_reply(names, products, message, language, size=None, *, compact=False):
    by_name = {item["name"].casefold(): item for item in products}
    lines = []
    for name in names:
        product = by_name.get(name.casefold())
        if product is None:
            lines.append(f"{name}: модель отсутствует в каталоге." if language == "ru"
                         else f"{name}: моделі немає в каталозі.")
            continue
        amount, currency = _price_for_size(product, size)
        price = f"{amount:g} {currency}"
        sizes = ", ".join(f"{size:g}" for size in product.get("available_sizes", [])) or "не указаны"
        stock = product.get("total_stock")
        stock_by_size = [
            f"EU {variant['size']:g}: {variant['stock']}"
            for variant in product.get("variants", []) if variant.get("size") is not None
        ]
        stock_text = f"{stock}" if stock is not None else "не указан"
        if stock_by_size:
            stock_text += " (" + "; ".join(stock_by_size) + ")"
        if compact:
            color = COLOR_LABELS[language].get(str(product.get("color", "")).lower(), product.get("color") or "—")
            if size is not None:
                variant = next((item for item in product["variants"] if item["size"] == size), None)
                stock = variant["stock"] if variant else 0
                availability = f"EU {size:g}: " + (
                    (_pair_count(stock, language) + (" в наличии" if language == "ru" else " у наявності"))
                    if stock > 0 else ("нет в наличии" if language == "ru" else "немає в наявності"))
            else:
                availability = ("размеры " if language == "ru" else "розміри ") + sizes
            lines.append(f"{product['brand']} {product['name']} — {price}, {color}, {availability}.")
            continue
        fields = [f"цена {price}", f"доступные размеры EU {sizes}", f"остаток {stock_text}"]
        for label, key in (("назначение", "use_case"), ("цвет", "color"), ("пол", "gender")):
            value = product.get(key)
            fields.append(f"{label} {value}" if value else f"{label} не указан в каталоге")
        features = product.get("features") or []
        fields.append("характеристики " + ", ".join(map(str, features)) if features
                      else "дополнительные характеристики не указаны в каталоге")
        lines.append(f"{product['brand']} {product['name']}: " + "; ".join(fields) + ".")

    if re.search(r"дешев\w*|дешевш\w*|ціна|цен\w*", message, re.I):
        if len(products) >= 2 and len({_price_for_size(item, size)[1] for item in products}) == 1:
            cheaper = min(products, key=lambda item: _price_for_size(item, size)[0])
            amount, currency = _price_for_size(cheaper, size)
            if len({_price_for_size(item, size)[0] for item in products}) == 1:
                lead = "По цене они равны." if language == "ru" else "За ціною вони однакові."
            else:
                lead = (f"Дешевле {cheaper['brand']} {cheaper['name']} — "
                        f"{amount:g} {currency}." if language == "ru" else
                        f"Дешевша модель {cheaper['brand']} {cheaper['name']} — "
                        f"{amount:g} {currency}.")
            if _price_choice(message):
                if size is not None:
                    variant = next((item for item in cheaper["variants"] if item["size"] == size), None)
                    stock = variant["stock"] if variant else 0
                    lead += (f" EU {size:g}: в наличии {_pair_count(stock, language)}." if language == "ru" else
                             f" EU {size:g}: у наявності {_pair_count(stock, language)}.") if stock > 0 else (
                             f" EU {size:g} нет в наличии." if language == "ru" else f" EU {size:g} немає в наявності.")
                return lead
            return lead + " " + " ".join(lines)
        lead = "Сравнить цены нельзя: в каталоге нет обеих моделей или общей валюты." if language == "ru" else \
            "Не можу порівняти ціни: у каталозі немає обох моделей або спільної валюти."
        return lead + " " + " ".join(lines)
    if re.search(r"размер\w*|розмір\w*|size", message, re.I):
        return "Размеры, доступные сейчас по каталогу: " + " ".join(lines) if language == "ru" else \
            "Розміри, доступні зараз за каталогом: " + " ".join(lines)
    intro = "Сравнение по данным каталога: " if language == "ru" else "Порівняння за даними каталогу: "
    return intro + " ".join(lines)


def _search_with_fallbacks(db, state, filters, *, lowercased, cheaper, language, notes, similar_anchor=None, shopping: bool = True, cheaper_reference: float | None = None, preferences: dict | None = None, hard_constraints: dict | None = None, display_currency: str = "USD") -> dict:
    """Основная выдача: поиск, контролируемое ослабление условий и честный ответ.

    Ослабленные условия никогда не записываются в состояние диалога: сказанное пользователем
    остаётся в силе, а альтернативы показываются как альтернативы и называются в ответе.
    Размер и наличие не ослабляются: показывается только то, что реально есть в наличии.
    """
    copy = COPY[language]
    more_requested = _is_more_request(lowercased)
    # «Ещё» и «похожие» не должны повторять уже показанное.
    # Исключение уже показанного работает только в товарном контексте: нетоварная фраза
    # не может «продолжить» выдачу и получить ложное «больше моделей нет».
    excluded_ids = set(state.get("shown_product_ids", [])) if (shopping and (more_requested or similar_anchor)) else set()
    requested_summary = _filter_summary(filters, language)
    preferences = preferences or {}
    hard_constraints = hard_constraints or {}
    query_filters = dict(filters)
    query_filters["display_currency"] = display_currency
    if (query_filters.get("min_price") is not None or query_filters.get("max_price") is not None) and not query_filters.get("currency"):
        query_filters["currency"] = display_currency
    if preferences:
        products = [item for item in search_products(db, **query_filters) if item["id"] not in excluded_ids]
        products = _rank_soft_preferences(products, preferences)[:_RESULT_LIMIT]
    else:
        products = recommend_products(db, exclude_ids=excluded_ids, **query_filters)
    if filters.get("sku_query"):
        sku = str(filters["sku_query"]).strip().lower()
        matches = [
            (product, variant)
            for product in products
            for variant in product.get("variants", [])
            if str(variant.get("sku", "")).strip().lower() == sku
        ]
        state["products"] = [product for product, _ in matches]
        if state["products"]:
            state["last_successful_products"] = state["products"]
            state["last_successful_product_ids"] = [item["id"] for item in state["products"]]
        state["shown_product_ids"].extend(
            product["id"] for product, _ in matches if product["id"] not in state["shown_product_ids"]
        )
        _with_reasons(state["products"], filters, language)
        if not matches:
            reply = (f"Артикул «{filters['sku_query']}» в каталоге не найден; другие товары по нему не подбирал."
                     if language == "ru" else
                     f"Артикул «{filters['sku_query']}» у каталозі не знайдено; інші товари за ним не підбирав.")
            return {"reply": _join_notes(notes, reply), "products": [], "needs_clarification": True,
                    "suggestions": []}
        product, variant = matches[0]
        currency = product.get("currency", product.get("base_currency", "USD"))
        symbol = _CURRENCY_SYMBOLS.get(currency, currency + " ")
        price = float(product.get("price", variant.get("price", 0)))
        size = variant.get("size")
        size_text = f", размер EU {size:g}" if size is not None else ""
        if variant.get("stock", 0) > 0:
            reply = (f"Артикул {sku}: {product['brand']} {product['name']}{size_text}, "
                     f"цена {symbol}{price:g}, остаток {variant['stock']}." if language == "ru" else
                     f"Артикул {sku}: {product['brand']} {product['name']}{size_text}, "
                     f"ціна {symbol}{price:g}, залишок {variant['stock']}.")
        else:
            reply = (f"Артикул {sku}: {product['brand']} {product['name']}{size_text}, "
                     f"цена {symbol}{price:g}; этого варианта нет в наличии." if language == "ru" else
                     f"Артикул {sku}: {product['brand']} {product['name']}{size_text}, "
                     f"ціна {symbol}{price:g}; цього варіанта немає в наявності.")
        return {"reply": _join_notes(notes, reply), "products": state["products"],
                "needs_clarification": False, "suggestions": []}
    if filters.get("name_query"):
        # A named model/generation must never be silently replaced by another model.
        state["products"] = products
        if products:
            state["last_successful_products"] = products
            state["last_successful_product_ids"] = [item["id"] for item in products]
        state["shown_product_ids"].extend(item["id"] for item in products if item["id"] not in state["shown_product_ids"])
        _with_reasons(products, filters, language)
        names = list(dict.fromkeys(f"{item['brand']} {item['name']}" for item in products))
        if len(products) == 1 and re.search(
                r"сколько\s+стоит|скільки\s+коштує|цен[ауы]|ц[іi]н[ауы]|налич|наявн|сколько\s+пар|остаток|залишок",
                lowercased):
            product = products[0]
            variant = next((item for item in product.get("variants", [])
                            if item.get("size") == filters.get("size")), None)
            price, currency = product["price"], product["currency"]
            if variant is not None:
                currency = product.get("base_currency") or currency
                price = variant["price"]
                converted = convert_amount(price, currency, display_currency,
                                           get_rates().get("rates") if currency != display_currency else None)
                if converted is not None:
                    price, currency = converted, display_currency
            stock = variant["stock"] if variant is not None else product["total_stock"]
            size = f", EU {variant['size']:g}" if variant is not None else ""
            reply = (f"{product['brand']} {product['name']}{size}: "
                     f"{price:g} {currency}; "
                     + (f"в наличии {stock} пар." if language == "ru" else f"у наявності {stock} пар."))
        elif names:
            reply = ("У каталозі: " if language == "uk" else "В каталоге: ") + "; ".join(names) + "."
            if len(names) > 1:
                reply += " Можна обрати покоління або модель." if language == "uk" else "Можно выбрать поколение или модель."
        elif more_requested:
            reply = copy["no_more"]
        else:
            requested = f"{filters.get('brand', '')} {filters['name_query']}".strip()
            reply = (f"Модель «{requested}» за заданими умовами у каталозі відсутня." if language == "uk"
                     else f"Модель «{requested}» по заданным условиям в каталоге отсутствует.")
        return {"reply": _join_notes(notes, reply), "products": products, "needs_clarification": False, "suggestions": []}
    no_more = more_requested and not products
    soft_filters: dict = {}
    relaxed = relaxed_brand = broadest = False
    budget_relaxed = False
    nearby_sizes: list[float] = []
    if not products and not no_more and filters.get("color") and "color" not in hard_constraints:
        alternatives = recommend_products(
            db, limit=_RESULT_LIMIT, **{key: value for key, value in query_filters.items() if key != "color"}
        )
        if alternatives:
            products, relaxed = alternatives, True
    if (not products and not no_more and cheaper and (filters.get("brand") or filters.get("color"))
            and not ({"brand", "color"} & set(hard_constraints))):
        alternatives = recommend_products(
            db, limit=_RESULT_LIMIT, **{key: value for key, value in query_filters.items() if key not in {"brand", "color"}}
        )
        if alternatives:
            products, relaxed_brand = alternatives, True
    if not products and not no_more and "size" not in hard_constraints:
        products, nearby_sizes = _nearest_size_products(db, query_filters)
    if (not products and not no_more and "max_price" not in hard_constraints
            and "min_price" not in hard_constraints):
        products = [item for item in _near_budget_products(db, query_filters) if item["id"] not in excluded_ids]
        budget_relaxed = bool(products)
    if not products and not no_more:
        # Последний шаг: оставляем смысловые условия и снимаем бренд, цвет и цену — и говорим об этом.
        # Исключения остаются: «не Nike» — это часть запроса, а не сужение выдачи.
        soft_filters = {key: value for key, value in query_filters.items() if key in _SOFT_KEPT_KEYS or key == "currency"}
        soft_filters.update({key: value for key, value in query_filters.items() if key in _SOFT_KEPT_EXCLUSIONS})
        soft_filters.update({key: query_filters[key] for key in hard_constraints if key in query_filters})
        candidates = recommend_products(db, exclude_ids=excluded_ids, limit=_RESULT_LIMIT, **soft_filters)
        if candidates:
            products, broadest = candidates, True
    state["products"] = products
    if products:
        state["last_successful_products"] = products
        state["last_successful_product_ids"] = [item["id"] for item in products]
    state["shown_product_ids"].extend(product["id"] for product in products if product["id"] not in state["shown_product_ids"])
    _with_reasons(products, filters, language, nearest=bool(nearby_sizes))
    summary = _filter_summary(filters, language)
    # A relative price query must explain an empty result without inventing a new budget.
    if cheaper and cheaper_reference is not None:
        cheapest = min((float(product["price"]) for product in products), default=None)
        if cheapest is None or cheapest >= cheaper_reference:
            notes.append(copy["cheaper_none"])
    if not products and cheaper and cheaper_reference is not None:
        reply = ""  # The precise reason is already present in notes.
    elif no_more:
        reply = copy["no_more"]
    elif nearby_sizes:
        sizes = ", ".join(f"{size:g}" for size in nearby_sizes)
        reply = copy["nearby_size"].format(size=float(filters["size"]).__format__("g"), sizes=sizes)
    elif budget_relaxed:
        reply = copy["over_budget"]
    elif relaxed:
        reply = copy["relaxed_color"].format(summary=requested_summary, count=_variant_count(len(products), language))
    elif relaxed_brand:
        reply = copy["relaxed_brand"].format(count=_variant_count(len(products), language))
    elif broadest:
        exclusions = _exclusion_summary(soft_filters, language)
        # Если исключение сохранено, нельзя обещать «без ограничений по бренду и цвету»:
        # отдельная формулировка называет исключение прямо.
        reply = (copy["broadest_excluded"] if exclusions else copy["broadest"]).format(
            summary=requested_summary,
            count=_variant_count(len(products), language),
            kept=_kept_conditions_phrase(soft_filters, language),
            excluded=exclusions,
        )
    elif similar_anchor and products:
        reply = copy["similar"].format(anchor=similar_anchor, count=_variant_count(len(products), language))
    elif not products:
        # Честное «нет»: называем не только факт, но и реальный рычаг в текущих условиях,
        # а если причины в размере — ближайшие размеры, которые в каталоге действительно есть.
        relax = _relax_phrase(filters, language)
        sizes = _size_options(db, filters)
        if sizes:
            reply = copy["no_results_size"].format(
                summary=summary, sizes=", ".join(f"{size:g}" for size in sizes), relax=relax or copy["size"]
            )
        elif relax:
            reply = copy["no_results_relax"].format(summary=summary, relax=relax)
        else:
            reply = copy["no_results"].format(summary=summary)
    elif more_requested:
        reply = copy["more"]
    elif products and is_recommendation_request(lowercased):
        top = products[0]
        reply = copy["recommend_data"].format(
            product=f"{top['brand']} {top['name']}", reasons=", ".join(top.get("match_reasons", []))
        )
    elif "есть" in lowercased or "є" in lowercased:
        reply = copy["stock"]
    elif summary:
        reply = copy["found"].format(count=_variant_count(len(products), language), summary=summary)
    else:
        reply = copy["found_all"].format(count=_variant_count(len(products), language))
    if similar_anchor and similar_anchor not in reply:
        reply = (f"Варианты вместо {similar_anchor}. " if language == "ru" else
                 f"Варіанти замість {similar_anchor}. ") + reply
    result = {"reply": _join_notes(notes, reply), "products": products, "needs_clarification": bool(nearby_sizes)}
    if nearby_sizes:
        result["suggestions"] = [f"Покажи {size:g}" for size in nearby_sizes]
    elif not products and not no_more:
        # Вместо общих подсказок — кнопки по условиям, которые действительно стоят в запросе.
        result["suggestions"] = _relax_suggestions(filters, language)
    return result


def _finish(result, state, provider, provider_failed, intent, filters, fallback_filters, ai_filters, language) -> dict:
    """Дописывает служебные поля ответа: режим работы, подсказки и историю диалога."""
    # Модель считается повлиявшей на выборку, если её фильтр остался в итоговых условиях,
    # а детерминированный парсер сам поставил бы другое значение (или не поставил бы вовсе).
    ai_contribution = {key: value for key, value in ai_filters.items() if filters.get(key) == value and fallback_filters.get(key) != value}
    if os.getenv("AI_DEBUG", "").lower() in {"1", "true"}:
        logging.getLogger("uvicorn.error").info(
            "AI provider=%s intent=%s filters=%s ai_contribution=%s provider_failed=%s tools=recommend_products",
            type(provider).__name__ if provider else "fallback", intent, filters, ai_contribution, provider_failed,
        )
    ai_reply_used = bool(result.pop("_ai_reply_used", False))
    result["mode"] = "ollama" if ai_contribution or ai_reply_used else "fallback"
    # Условия ответа для интерфейса: нужны, чтобы подсветить выбранный размер и честно сказать,
    # если его нет. Решение о размере по-прежнему принимает backend, фронтенд только отображает.
    result["filters"] = {key: value for key, value in filters.items() if key != "name_query"}
    result.setdefault("suggestions", _suggestions(bool(result["products"]), result["needs_clarification"], language))
    record_reply(state, result)
    state["history"].append({"role": "assistant", "content": result["reply"]})
    state["history"] = state["history"][-12:]
    return result
