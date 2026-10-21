"""Catalog-backed handling of measurements, model names and conflicting conditions."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from sqlalchemy import select

from ..ai.tools import search_products
from ..catalog import base_query
from ..catalog_rules import BRAND_ALIASES, COLOR_CANONICAL, GENDER_CANONICAL, MODEL_ALIASES, USE_CASE_CANONICAL
from ..models import Product

_LENGTH = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(?:см|cm|jp)\b|\b(?:cm(?:/jp)?|jp)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", re.I)
_KIND = re.compile(r"устіл\w*|стель\w*|стоп\w*|\bfoot\b|\binsole\b|cm/jp|\bjp\b", re.I)


def measurement_kind(text):
    matches = list(_KIND.finditer(text))
    if not matches:
        return None
    word = matches[-1].group().lower()
    if word.startswith(("устіл", "стель", "insole")):
        return "insole_cm"
    if word.startswith(("стоп", "foot")):
        return "foot_cm"
    return "cm_jp"


def update_measurement(state, message, explicit_size=None):
    clarification = state.setdefault("clarification", {})
    match = _LENGTH.search(message)
    if match:
        # The nearest preceding label describes this measurement; otherwise use its suffix.
        kind = (measurement_kind(message[:match.start()]) or measurement_kind(match.group())
                or measurement_kind(message[match.end():]))
        value = (match.group(1) or match.group(2)).replace(",", ".")
        measurement = {"value": value, "kind": kind}
        previous = clarification.get("measurement") or {}
        if previous.get("value") == value and not kind:
            measurement["kind"] = previous.get("kind")
        if any(previous.get(key) != measurement.get(key) for key in ("value", "kind")):
            clarification.pop("asked_length", None)
            clarification.pop("asked_eu", None)
        clarification["measurement"] = measurement
        clarification.pop("measurement_deferred", None)
        clarification.pop("explicit_eu", None)
    else:
        kind = measurement_kind(message)
        if kind:
            previous = clarification.get("measurement") or {"value": None}
            if previous.get("kind") != kind:
                clarification.pop("asked_eu", None)
                clarification.pop("asked_length", None)
            clarification["measurement"] = {**previous, "kind": kind}
            clarification.pop("measurement_deferred", None)
    if explicit_size is not None:
        clarification["explicit_eu"] = explicit_size
    elif state.get("filters", {}).get("size") is not None:
        clarification["explicit_eu"] = state["filters"]["size"]
    return clarification.get("measurement")


def catalog_model_filters(db, message):
    """Exact known catalog titles, including those absent from static aliases."""
    text = re.sub(r"\s+", " ", message.lower())
    products = db.scalars(select(Product).where(Product.active.is_(True))).all()
    matches = [
        product for product in products
        if re.search(r"(?<!\w)" + re.escape(product.name.lower()) + r"(?!\w)", text)
    ]
    if not matches:
        # An explicit brand + model title remains a model lookup even when absent.
        for alias in sorted(BRAND_ALIASES, key=len, reverse=True):
            hit = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text)
            if not hit:
                continue
            tail = re.split(r"\s+\d+(?:[.,]\d+)?\s*(?:см|cm|jp)\b|[,;?!]|\b(?:размер\w*|розмір\w*|eu|цвет\w*|колір\w*|до|от|від|для|стель\w*|устіл\w*|стоп\w*)\b", text[hit.end():], maxsplit=1)[0].strip()
            attribute_value = any(tail in aliases or tail in {str(value).casefold() for value in aliases.values()}
                                  for aliases in (COLOR_CANONICAL, GENDER_CANONICAL, USE_CASE_CANONICAL))
            if tail and not attribute_value and not (tail.isdigit() and len(tail) < 3) and re.fullmatch(r"[a-z0-9][a-z0-9 '’.-]*", tail):
                return {"brand": BRAND_ALIASES[alias], "name_query": tail}
        return {}
    product = max(matches, key=lambda item: len(item.name))
    return {"brand": product.brand, "name_query": product.name}


_COMPARE_INTENT = re.compile(r"\b(?:сравни\w*|порівняй\w*|compare|сравнение|порівняння)\b", re.I)
_COMPARE_SPLIT = re.compile(r"\s+(?:против|проти|vs\.?|versus|и|с|і|й|та|з|із)\s+", re.I)


def comparison_model_names(db, message):
    """Extract two requested model titles; keep unknown names so absence is explicit."""
    if not _COMPARE_INTENT.search(message):
        return []
    text = re.sub(r"\s+", " ", message).strip()
    text = re.sub(r"^.*?\b(?:сравни\w*|порівняй\w*|compare|сравнение|порівняння)\b", "", text, flags=re.I)
    text = re.sub(r"[?!.,;:]+$", "", text).strip()
    parts = _COMPARE_SPLIT.split(text, maxsplit=1)
    if len(parts) != 2:
        return []

    products = db.scalars(select(Product).where(Product.active.is_(True))).all()
    titles = sorted({product.name for product in products}, key=len, reverse=True)
    aliases = sorted(set(MODEL_ALIASES), key=len, reverse=True)
    result = []
    for part in parts:
        candidate = part.strip()
        found = next((title for title in titles
                      if re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", candidate, re.I)), None)
        if found is None:
            found = next((MODEL_ALIASES[alias] for alias in aliases
                          if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", candidate, re.I)), None)
        if found is None:
            model_part = candidate
            for alias in sorted(BRAND_ALIASES, key=len, reverse=True):
                model_part = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", " ", model_part, flags=re.I)
            model_part = re.sub(r"\s+", " ", model_part).strip()
            partials = [title for title in titles if title.lower().startswith(model_part.lower() + " ")]
            if len(partials) == 1:
                found = partials[0]
        if found is None:
            for alias in sorted(BRAND_ALIASES, key=len, reverse=True):
                candidate = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", " ", candidate, flags=re.I)
            candidate = re.sub(r"\b(?:модель|кроссовки|sneakers|please|пожалуйста)\b", " ", candidate, flags=re.I)
            found = re.sub(r"\s+", " ", candidate).strip(" -–—")
        if found:
            result.append(found)
    return result if len(result) == 2 and result[0].casefold() != result[1].casefold() else []


def conflicting_conditions(filters):
    labels = []
    minimum, maximum = filters.get("min_price"), filters.get("max_price")
    if minimum is not None and maximum is not None and minimum > maximum:
        labels.append("price")
    if filters.get("brand") in (filters.get("exclude_brand") or []):
        labels.append("brand")
    axes = {"color": "color", "size": "size", "purpose": "use_case", "tags": "feature"}
    for code, excluded in (filters.get("exclude_attributes") or {}).items():
        included = filters.get(axes.get(code, code))
        values = included if isinstance(included, (list, tuple)) else [included]
        rejected = excluded if isinstance(excluded, (list, tuple)) else [excluded]
        if included is not None and any(value in rejected for value in values):
            labels.append(axes.get(code, code))
    return labels


def conflict_response(state, filters, language):
    conflicts = conflicting_conditions(filters)
    if not conflicts:
        state.setdefault("clarification", {}).pop("conflict_question", None)
        return None
    uk = language == "uk"
    signature = repr(sorted(filters.items()))
    clarification = state.setdefault("clarification", {})
    repeated = clarification.get("conflict_question") == signature
    clarification["conflict_question"] = signature
    if "price" in conflicts:
        text = "Мінімальна ціна більша за максимальну." if uk else "Минимальная цена больше максимальной."
        question = "Яку межу бюджету змінити?" if uk else "Какую границу бюджета изменить?"
    else:
        text = ("Умову ви назвали й одночасно виключили." if uk
                else "Условие вы назвали и одновременно исключили.")
        question = "Яку суперечливу умову змінити?" if uk else "Какое противоречивое условие изменить?"
    if not repeated:
        text += " " + question
    return {"reply": text, "products": [], "needs_clarification": True, "suggestions": [],
            "_question": "budget_boundary" if "price" in conflicts else "condition"}


def _attributes(owner):
    return {row.attribute.code: row.value for row in owner.attributes}


def has_verified_chart(product):
    meta = _attributes(product)
    source = urlsplit(str(meta.get("size_chart_source", "")))
    return bool(
        str(meta.get("size_chart_verified", "")).lower() in {"true", "1"}
        and meta.get("size_chart_brand") == product.brand
        and meta.get("size_chart_model") == product.name
        and source.scheme == "https" and source.netloc
    )


def verified_measurement_sizes(product, measurement):
    """Use only curated rows for the exact brand/model; never infer a conversion."""
    if not has_verified_chart(product):
        return []
    sizes = []
    for variant in product.variants:
        attrs = _attributes(variant)
        if variant.stock <= 0 or measurement["kind"] not in attrs:
            continue
        try:
            length = Decimal(str(attrs[measurement["kind"]]).replace(",", "."))
            size = Decimal(str(attrs.get("size")))
            expected = Decimal(measurement["value"])
            if length.is_finite() and size.is_finite() and length == expected:
                sizes.append(float(size))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return sorted(set(sizes))


def missing_model_response(db, filters, language):
    name = filters.get("name_query")
    if not name:
        return None
    pattern = r"(?<!\w)" + r"\s*".join(re.escape(word) for word in name.lower().split()) + r"(?!\w)"
    if any(re.search(pattern, product.name.lower()) and (
            not filters.get("brand") or product.brand.casefold() == filters["brand"].casefold())
            for product in db.scalars(base_query()).unique().all()):
        return None
    title = f"{filters.get('brand', '')} {name}".strip()
    return {"reply": (f"Модель «{title}» у каталозі відсутня." if language == "uk" else
                      f"Модель «{title}» в каталоге отсутствует."),
            "products": [], "needs_clarification": False, "suggestions": []}


def measurement_response(db, state, filters, language, display_currency, more=False):
    clarification = state.setdefault("clarification", {})
    measurement = clarification.get("measurement")
    if not measurement or clarification.get("measurement_deferred"):
        return None
    if measurement.get("value") is not None and clarification.get("explicit_eu") is not None:
        return None
    query = {**filters, "display_currency": display_currency}
    candidates = search_products(db, **query)
    stored = {product.id: product for product in db.scalars(base_query()).unique().all()}
    has_chart = any(has_verified_chart(stored[item["id"]]) for item in candidates)
    if measurement.get("value") is None:
        if not has_chart:
            return {"reply": ("У каталозі немає перевіреної таблиці цієї моделі; довжину підтвердити не можу." if language == "uk" else
                              "В каталоге нет проверенной таблицы этой модели; длину подтвердить не могу."),
                    "products": [], "needs_clarification": False, "suggestions": []}
        if filters.get("size") is None:
            return {"reply": ("Для якого EU-розміру перевірити довжину?" if language == "uk" else
                              "Для какого EU-размера проверить длину?"),
                    "products": [], "needs_clarification": True, "_question": "eu_size", "suggestions": []}
        lines, confirmed = [], []
        for item in candidates:
            product = stored[item["id"]]
            if not has_verified_chart(product):
                continue
            for variant in product.variants:
                attrs = _attributes(variant)
                value = attrs.get(measurement["kind"])
                try:
                    length = Decimal(str(value).replace(",", "."))
                    same_size = Decimal(str(attrs.get("size"))) == Decimal(str(filters["size"]))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if length.is_finite() and length > 0 and same_size:
                    label = {"insole_cm": ("устілка", "стелька"), "foot_cm": ("стопа", "стопа"), "cm_jp": ("CM/JP", "CM/JP")}[measurement["kind"]][0 if language == "uk" else 1]
                    lines.append(f"{item['brand']} {item['name']}, EU {filters['size']:g}: {label} {length:g} см.")
                    item["match_reasons"] = [f"{item['brand']} {item['name']}: {label} {length:g} см — EU {filters['size']:g}"]
                    confirmed.append(item)
        return {"reply": " ".join(lines) if lines else ("Для цього EU-розміру перевіреної довжини немає." if language == "uk" else
                                                         "Для этого EU-размера проверенной длины нет."),
                "products": confirmed, "needs_clarification": False, "suggestions": []}
    if not measurement.get("kind") and has_chart:
        first = not clarification.get("asked_length")
        clarification["asked_length"] = True
        return {
            "reply": ("Це довжина стопи чи устілки?" if language == "uk" else "Это длина стопы или стельки?")
            if first else ("Тип вимірювання ще не уточнений." if language == "uk" else "Тип измерения ещё не уточнён."),
            "products": [], "needs_clarification": True, "suggestions": [],
            "_question": "measurement_kind",
        }
    excluded = set(state.get("shown_product_ids", [])) if more else set()
    matches = []
    for product in candidates:
        sizes = verified_measurement_sizes(stored[product["id"]], measurement) if measurement.get("kind") else []
        if filters.get("size") is not None:
            sizes = [size for size in sizes if size == filters["size"]]
        if sizes and product["id"] not in excluded:
            product["matching_sizes"] = sizes
            label = {"insole_cm": "устілки", "foot_cm": "стопи", "cm_jp": "CM/JP"}[measurement["kind"]] if language == "uk" else {"insole_cm": "стельки", "foot_cm": "стопы", "cm_jp": "CM/JP"}[measurement["kind"]]
            product["match_reasons"] = [
                f"{product['brand']} {product['name']}: {label} {measurement['value']} см — EU " + ", ".join(f"{size:g}" for size in sizes)
            ]
            matches.append(product)
    if matches:
        reply = ("Розміри підтверджені таблицями конкретних моделей." if language == "uk"
                 else "Размеры подтверждены таблицами конкретных моделей.")
        return {"reply": reply, "products": matches[:3], "needs_clarification": False}
    if more and candidates and any(verified_measurement_sizes(stored[item["id"]], measurement) for item in candidates):
        return {"reply": "Більше підтверджених варіантів немає." if language == "uk" else "Больше подтверждённых вариантов нет.", "products": [], "needs_clarification": False}
    first = not clarification.get("asked_eu")
    clarification["asked_eu"] = True
    reply = ("Достовірної таблиці для підтвердження цього вимірювання у відповідних моделей каталогу немає; точний розмір підтвердити не можу."
             if language == "uk" else "Достоверной таблицы для подтверждения этого измерения у подходящих моделей каталога нет; точный размер подтвердить не могу.")
    if has_chart:
        reply = ("За таблицями відповідних моделей немає підтвердженого варіанта в наявності для цього вимірювання."
                 if language == "uk" else "По таблицам подходящих моделей нет подтверждённого варианта в наличии для этого измерения.")
    if first:
        reply += " Який перевірений EU-розмір шукати?" if language == "uk" else " Какой проверенный EU-размер искать?"
    return {"reply": reply, "products": [], "needs_clarification": True, "suggestions": [],
            "_question": "eu_size"}

