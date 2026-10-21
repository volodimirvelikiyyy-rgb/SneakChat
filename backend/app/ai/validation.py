"""Ворота между моделью и поиском.

Ни одно значение, предложенное моделью, не попадает в выборку без проверки. Проверка отвечает
на два вопроса: «правильного ли оно типа» и «есть ли оно в этом каталоге». Словарную
канонизацию и границы цены делает следующий слой (`chat._normalize_ai_filters`), поэтому
ворота не дублируют его работу, а добавляют то, чего там нет, — сверку с живыми данными.

Сверка идёт по данным, а не по списку в коде: набор допустимых значений берётся из объявленных
атрибутов активного каталога и из самих записей. Поэтому второй каталог проверяется теми же
правилами без правок, а значение, которого в каталоге сейчас нет, не превращается в фильтр.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..catalog import catalogue_brands, declared_attribute_values
from ..catalog_rules import (
    ATTRIBUTE_VOCABULARIES, BRAND_CANONICAL, PRICE_KEYS, SNEAKER_CONFIG, match_value,
)
from ..schemas import PRICE_MAX, PRICE_MIN, sanitize_price

# Интенты, которые приложение умеет обслуживать. Всё остальное — не команда, а мусор.
ALLOWED_INTENTS = (
    "search_products",
    "check_stock",
    "product_details",
    "compare_products",
    "recommendation",
    "clarification",
)
DEFAULT_INTENT = "search_products"

# Причины отказа. Хранятся, чтобы отказ не был молчаливым: по ним пишется debug-лог и
# проверяются тесты, которые обязаны доказать, что значение не дошло до выборки.
REASON_WRONG_TYPE = "wrong_type"
REASON_OUT_OF_RANGE = "out_of_range"
REASON_UNKNOWN_ATTRIBUTE = "unknown_attribute"
REASON_UNKNOWN_VALUE = "unknown_value"
REASON_INVALID_INTENT = "invalid_intent"
REASON_UNEXPECTED_KEY = "unexpected_key"
# Нарушения контракта: их нельзя исправить частично, ответ модели перестаёт заслуживать доверия.
CONTRACT_REASONS = (REASON_UNEXPECTED_KEY, REASON_INVALID_INTENT)

# Ключи, которые модель имеет право вернуть. Всё остальное — попытка добавить себе поле,
# например текст ответа или «цену», поэтому такой разбор не используется.
ALLOWED_KEYS = ("intent", "filters", "evidence")


@dataclass(frozen=True)
class Rejection:
    """Одно отклонённое предложение модели."""

    key: str
    value: object
    reason: str

    def describe(self) -> str:
        return f"{self.key}={self.value!r} ({self.reason})"


@dataclass
class ValidatedAnalysis:
    """Результат проверки: что модель получила право сказать и что было отклонено."""

    intent: str = DEFAULT_INTENT
    filters: dict = field(default_factory=dict)
    rejections: list[Rejection] = field(default_factory=list)

    @property
    def rejected(self) -> list[str]:
        return [item.describe() for item in self.rejections]

    @property
    def contract_broken(self) -> bool:
        """Ответ модели нарушил договор — доверять ему нельзя даже частично."""
        return any(item.reason in CONTRACT_REASONS for item in self.rejections)


def _as_number(value: object) -> float | None:
    """Число из значения модели.

    Модели часто отдают число строкой («43», «120»), и такое значение однозначно, поэтому
    оно принимается. Строка со словами («до 120», «около сотки») не принимается: неизвестно,
    что именно она ограничивает, а детерминированный парсер разбирает такие формулировки сам.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        try:
            number = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _as_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _axis_values(db: Session, config) -> dict[str, set[str]]:
    """Живые значения каждого объявленного атрибута в нижнем регистре."""
    return {
        code: {item.strip().lower() for item in values if item and item.strip()}
        for code, values in declared_attribute_values(db, config).items()
    }


def _canonical(axis: str, code: str, value: str) -> str:
    """Канонический вид значения по словарю каталога; неизвестное значение возвращается как есть.

    Словарь ищется по коду атрибута каталога («purpose», «tags»), а если его там нет — по
    имени оси приложения («use_case», «feature»). Так одно значение словаря обслуживает и
    модель, и проверку, и подписи ответов.
    """
    aliases = ATTRIBUTE_VOCABULARIES.get(code)
    if aliases:
        matched = match_value(value, aliases)
        if matched is not None:
            return matched
    return value


def _validate_axis(axis: str, value: object, live: dict[str, set[str]] | None, config) -> tuple[str | None, Rejection | None]:
    """Проверяет одно категориальное значение против живого каталога.

    `live is None` означает, что сверять не с чем (каталог недоступен): тогда значение
    канонизируется, но не отбрасывается — отказ по отсутствию данных был бы выдумкой.
    """
    text = _as_text(value)
    if text is None:
        return None, Rejection(axis, value, REASON_WRONG_TYPE)
    code = config.attribute_code(axis)
    canonical = _canonical(axis, code, text)
    if live is None:
        return canonical, None
    allowed = live.get(code)
    if allowed is None:
        # Ось объявлена в словаре приложения, но такого атрибута у каталога нет.
        return None, Rejection(axis, value, REASON_UNKNOWN_ATTRIBUTE)
    if canonical.strip().lower() not in allowed:
        return None, Rejection(axis, value, REASON_UNKNOWN_VALUE)
    return canonical, None


def validate_analysis(db: Session | None, analysis: dict, config=SNEAKER_CONFIG) -> ValidatedAnalysis:
    """Проверяет разбор запроса от модели и возвращает безопасный набор фильтров.

    На выходе остаются только те значения, которые каталог действительно может обслужить.
    Отклонённые предложения не исчезают бесследно: они возвращаются списком причин.
    """
    result = ValidatedAnalysis()
    if not isinstance(analysis, dict):
        result.rejections.append(Rejection("analysis", analysis, REASON_WRONG_TYPE))
        return result

    for key in analysis:
        if key not in ALLOWED_KEYS:
            # Модель попыталась вернуть лишнее поле: разбор не используется целиком.
            result.rejections.append(Rejection(str(key), analysis[key], REASON_UNEXPECTED_KEY))

    intent = analysis.get("intent")
    if intent in ALLOWED_INTENTS:
        result.intent = intent
    else:
        result.rejections.append(Rejection("intent", intent, REASON_INVALID_INTENT))

    raw_filters = analysis.get("filters")
    if not isinstance(raw_filters, dict):
        if raw_filters is not None:
            result.rejections.append(Rejection("filters", raw_filters, REASON_WRONG_TYPE))
        return result

    live = _axis_values(db, config) if db is not None else None
    live_brands = {brand.strip().lower() for brand in catalogue_brands(db)} if db is not None else None
    size_spec = config.spec(config.primary_axis) if config.primary_axis else None

    for key, value in raw_filters.items():
        if value is None or value == "" or value == [] or value == {}:
            continue

        if key == "brand":
            text = _as_text(value)
            if text is None:
                result.rejections.append(Rejection(key, value, REASON_WRONG_TYPE))
                continue
            canonical = BRAND_CANONICAL.get(text.strip().lower(), text)
            if live_brands is not None and canonical.strip().lower() not in live_brands:
                result.rejections.append(Rejection(key, value, REASON_UNKNOWN_VALUE))
                continue
            result.filters[key] = canonical
            continue

        if key == "size":
            number = _as_number(value)
            if number is None:
                # В том числе «43 размера» и «до 43»: строку со словами разбирает парсер.
                result.rejections.append(Rejection(key, value, REASON_WRONG_TYPE))
                continue
            if size_spec is not None and (
                (size_spec.minimum is not None and number < size_spec.minimum)
                or (size_spec.maximum is not None and number > size_spec.maximum)
            ):
                # Размер вне оси каталога — не условие, а ошибка разбора.
                result.rejections.append(Rejection(key, value, REASON_OUT_OF_RANGE))
                continue
            result.filters[key] = number
            continue

        if key == "currency":
            if value not in {"USD", "EUR", "UAH"}:
                result.rejections.append(Rejection(key, value, REASON_WRONG_TYPE))
                continue
            result.filters[key] = value
            continue

        if key in PRICE_KEYS:
            number = _as_number(value)
            if number is None:
                result.rejections.append(Rejection(key, value, REASON_WRONG_TYPE))
                continue
            if sanitize_price(number) is None:
                result.rejections.append(Rejection(key, value, REASON_OUT_OF_RANGE))
                continue
            result.filters[key] = number
            continue

        if key == "attributes":
            if not isinstance(value, dict):
                result.rejections.append(Rejection(key, value, REASON_WRONG_TYPE))
                continue
            accepted: dict[str, object] = {}
            for code, item in value.items():
                code = str(code)
                if code not in {spec.code for spec in config.attributes}:
                    result.rejections.append(Rejection(f"attributes.{code}", item, REASON_UNKNOWN_ATTRIBUTE))
                    continue
                spec = config.spec(code)
                items = item if isinstance(item, (list, tuple)) else [item]
                kept: list[str] = []
                for single in items:
                    if spec is not None and spec.type == "number":
                        number = _as_number(single)
                        if number is None:
                            result.rejections.append(Rejection(f"attributes.{code}", single, REASON_WRONG_TYPE))
                            continue
                        # Границы оси проверяются и здесь: иначе size=99 прошёл бы как attributes.size.
                        if (spec.minimum is not None and number < spec.minimum) or (
                            spec.maximum is not None and number > spec.maximum
                        ):
                            result.rejections.append(Rejection(f"attributes.{code}", single, REASON_OUT_OF_RANGE))
                            continue
                        kept.append(f"{number:g}")
                        continue
                    canonical, rejection = _validate_axis(code, single, live, config)
                    if rejection is not None:
                        result.rejections.append(Rejection(f"attributes.{code}", single, rejection.reason))
                        continue
                    kept.append(str(canonical))
                if kept:
                    accepted[code] = kept[0] if len(kept) == 1 else kept
            if accepted:
                result.filters["attributes"] = accepted
            continue

        # Остальные категориальные оси приложения: цвет, пол, назначение, особенности.
        canonical, rejection = _validate_axis(key, value, live, config)
        if rejection is not None:
            result.rejections.append(rejection)
            continue
        result.filters[key] = canonical

    if result.filters.get("min_price") is not None and result.filters.get("max_price") is not None:
        if not PRICE_MIN <= result.filters["min_price"] <= result.filters["max_price"] <= PRICE_MAX:
            result.rejections.append(Rejection("price", (result.filters["min_price"], result.filters["max_price"]), REASON_OUT_OF_RANGE))
            result.filters.pop("min_price", None)
            result.filters.pop("max_price", None)

    return result
