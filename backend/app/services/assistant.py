"""Детерминированный разбор сообщения на фильтры каталога.

Словарь значений и шаблоны берутся из `catalog_rules` — того же источника, из которого
строится промпт для модели.
"""

import re

from sqlalchemy.orm import Session

from ..catalog_rules import (
    APPROXIMATE_WORDS, BRAND_CANONICAL, COLOR_CANONICAL, CURRENCY_WORDS, FEATURE_ALIAS_MAP,
    FEATURE_ALIASES, GENDER_ALIAS_MAP, MAX_PRICE_PREFIXES, MAX_SIZE, MIN_PRICE_PREFIXES, MIN_SIZE,
    MONEY_PATTERN, MONEY_RANGE_PATTERN, PRICE_PATTERN, PRICE_SEPARATOR, SIZE_NUMBER_AFTER_PATTERN,
    SIZE_PATTERN, SIZE_WORD_AFTER_PATTERN, SIZE_WORD_BEFORE_PATTERN, USE_CASE_ALIAS_MAP,
    MODEL_ALIASES, match_alias, negation_mask, size_context_mask, strict_phrase_hits,
)
from .products import find_products, serialize_product


def _is_size_phrase(text: str, match: re.Match) -> bool:
    """Число рядом со словом «размер» — это размер, а не цена («до 45 размера», «размер до 45»)."""
    numbers = re.findall(r"\d+(?:\.\d+)?", match.group(0))
    value = float(numbers[-1]) if numbers else None
    tail = text[match.end():]
    if SIZE_WORD_AFTER_PATTERN.match(tail):
        # «до 45 размер 43»: настоящий размер назван числом после слова — впереди стоит бюджет.
        if SIZE_NUMBER_AFTER_PATTERN.match(tail):
            return False
        # «до 150 размер 43»: 150 размером быть не может, значит это бюджет, а размер назван дальше.
        return value is None or MIN_SIZE <= value <= MAX_SIZE
    head = text[: match.start()]
    size_word = SIZE_WORD_BEFORE_PATTERN.search(head)
    if size_word is None:
        return False
    # «43 размера до 150»: число перед словом «размер» уже называет размер, а сумма после него — бюджет.
    return re.search(r"\d\s*$", head[: size_word.start()]) is None


def _strip_price_expressions(text: str) -> str:
    """Заменяет ценовые выражения пробелами, чтобы цена не читалась как размер."""
    characters = list(text)
    for pattern in (PRICE_PATTERN, MONEY_PATTERN):
        for match in pattern.finditer(text):
            if _is_size_phrase(text, match):
                continue
            for index in range(match.start(), match.end()):
                characters[index] = " "
    return "".join(characters)


def parse_request(message: str) -> dict[str, str | float]:
    # Исключённые значения вырезаются до разбора: «только не Nike» — это не фильтр по Nike.
    # Значение, названное ещё и вне отрицания («Nike, но не Nike»), остаётся, и диалог
    # сообщает о противоречии отдельно.
    text = negation_mask(size_context_mask(message)).lower().replace(",", ".")
    filters: dict[str, str | float] = {}
    # SKU is an exact variant identifier. Remove it from the text before parsing so its
    # model words and trailing size-like digits do not become ordinary filters.
    sku_match = re.search(
        r"\b(?:sku|артикул\w*)\s*[:#№-]?\s*"
        r"((?=[a-z0-9_-]*\d)[a-z0-9]+(?:[-_][a-z0-9]+)*)\b",
        message,
        re.IGNORECASE,
    )
    if sku_match is None:
        sku_match = re.search(
            r"(?<![\w-])(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)"
            r"[a-z0-9]+(?:[-_][a-z0-9]+)+(?![\w-])",
            message,
            re.IGNORECASE,
        )
    if sku_match is not None:
        token = sku_match.group(1) if sku_match.lastindex else sku_match.group(0)
        filters["sku_query"] = token.lower()
        text = text[:sku_match.start()] + (" " * (sku_match.end() - sku_match.start())) + text[sku_match.end():]
    if brand := match_alias(text, BRAND_CANONICAL):
        filters["brand"] = brand
    if model := match_alias(text, MODEL_ALIASES):
        filters["name_query"] = model
    # A family name does not imply a generation. Keep vN only when named explicitly.
    family = re.search(r"\b(?:fresh\s+foam(?:\s+x)?\s+)?1080(?:\s*(v\d+))?\b", text)
    if family:
        filters["name_query"] = "1080" + (" " + family.group(1) if family.group(1) else "")
        filters.setdefault("brand", "New Balance")
    if color := match_alias(text, COLOR_CANONICAL):
        filters["color"] = color
    if use_case := match_alias(text, USE_CASE_ALIAS_MAP):
        filters["use_case"] = use_case
    # Теги многозначны: «не промокали на осень» — это сразу два признака каталога.
    # Признаки ищутся тем же строгим правилом, что и остальные оси: подстрочное совпадение
    # превращало «дождаться» в водонепроницаемость.
    features = [code for code, phrases in FEATURE_ALIASES.items() if strict_phrase_hits(text, phrases)]
    if features:
        filters["feature"] = features[0] if len(features) == 1 else features
    if re.search(r"(?:€|\beur\b|евр\w*|євр\w*)", text):
        filters["currency"] = "EUR"
    elif re.search(r"(?:₴|\buah\b|\bгрн\w*|\bгрив\w*)", text):
        filters["currency"] = "UAH"
    elif re.search(r"(?:\$|\busd\b|доллар\w*|долар\w*)", text):
        filters["currency"] = "USD"
    size_text = _strip_price_expressions(text)
    size_text = re.sub(r"v\d+\b", " ", size_text)
    # Model generations such as Pegasus 41 are part of the exact name, not shoe sizes.
    # Remove only that recognized phrase so a separate "размер 41" remains parseable.
    name_query = str(filters.get("name_query") or "")
    if name_query:
        model_pattern = r"(?<!\w)" + r"\s+".join(re.escape(part) for part in name_query.lower().split()) + r"(?!\w)"
        size_text = re.sub(model_pattern, " ", size_text, count=1, flags=re.I)
    maximum = re.search(rf"(?:{MAX_PRICE_PREFIXES}){PRICE_SEPARATOR}\$?\s*(\d+(?:\.\d+)?)", text)
    if maximum and not _is_size_phrase(text, maximum):
        filters["max_price"] = float(maximum.group(1))
    currency_first = re.search(rf"(?:{CURRENCY_WORDS}){PRICE_SEPARATOR}(\d+(?:\.\d+)?)", text)
    if currency_first and "max_price" not in filters and not _is_size_phrase(text, currency_first):
        filters["max_price"] = float(currency_first.group(1))
    money_range = MONEY_RANGE_PATTERN.search(text)
    if money_range and "max_price" not in filters:
        low, high = float(money_range.group(1)), float(money_range.group(2))
        filters["min_price"], filters["max_price"] = min(low, high), max(low, high)
    currency_last = re.search(rf"(\d+(?:\.\d+)?){PRICE_SEPARATOR}(?:{CURRENCY_WORDS})", text)
    if currency_last and "max_price" not in filters and not _is_size_phrase(text, currency_last):
        filters["max_price"] = float(currency_last.group(1))
    for_price = re.search(r"\bза\s+\$?\s*(\d+(?:\.\d+)?)", text)
    if for_price and "max_price" not in filters and not _is_size_phrase(text, for_price):
        filters["max_price"] = float(for_price.group(1))
    minimum = re.search(rf"(?<!не )(?<!не)(?:{MIN_PRICE_PREFIXES}){PRICE_SEPARATOR}\$?\s*(\d+(?:\.\d+)?)", text)
    if minimum and not _is_size_phrase(text, minimum):
        filters["min_price"] = float(minimum.group(1))
        # The currency suffix of an explicit minimum is not a second, maximum bound.
        if (not maximum and currency_last and currency_last.start(1) == minimum.start(1)
                and filters.get("max_price") == filters["min_price"]):
            filters.pop("max_price", None)
    approximate = re.search(rf"(?:{APPROXIMATE_WORDS})\s*(?:за\s*)?\$?\s*(\d+(?:\.\d+)?)", text)
    if (approximate and "max_price" not in filters and "min_price" not in filters
            and not _is_size_phrase(text, approximate)):
        target = float(approximate.group(1))
        filters["min_price"], filters["max_price"] = max(0, target - 20), target + 20
    elif "за сотку" in text or "за сотню" in text:
        filters["min_price"], filters["max_price"] = 80.0, 120.0
    elif any(word in text for word in ("недорог", "недорогі")) and "max_price" not in filters:
        filters["max_price"] = 120.0
    for size_match in SIZE_PATTERN.finditer(size_text):
        size = float(size_match.group(1))
        if MIN_SIZE <= size <= MAX_SIZE:
            filters["size"] = size
            break
    if gender := match_alias(text, GENDER_ALIAS_MAP):
        filters["gender"] = gender
    return filters


def search_from_message(db: Session, message: str) -> tuple[dict[str, str | float], list[dict]]:
    filters = parse_request(message)
    products = find_products(db, **filters)
    return filters, [serialize_product(product) for product in products]
