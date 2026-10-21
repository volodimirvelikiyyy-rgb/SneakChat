"""Проверка, что каждое утверждение о товаре подтверждается backend.

Ответ собирается шаблонами и данными БД, поэтому модель в принципе не может написать текст о
товаре. Этот модуль — независимая проверка того, что получилось: он заново читает товары из
БД, сравнивает факты и ищет в тексте ответа суммы, которых нет ни у одного товара и ни в одном
условии запроса.

На каждом запросе проверка не нужна — она не влияет на ответ, а только доказывает его
правильность. Поэтому её вызывает набор тестов: если в шаблоне или подписи появится
выдуманная цифра или неверный факт, тест это покажет.
"""

import re

from sqlalchemy.orm import Session

from ..services.products import get_product, serialize_product
from ..services.currency import convert_amount, get_rates

# Суммы в тексте ответа: «до $120», «от $80», а также записанные словом валюты —
# «999 долларов», «120 грн». Размер и количество сюда не попадают.
# Обозначения суммы: символ валюты, слово (в любой раскладке — UAH/uah) или сокращение.
SYMBOL_MONEY = re.compile(r"[$€₴]\s*(\d+(?:\.\d+)?)")
WORD_MONEY = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:долл|долар|грн|грив|руб|евро|євро|бакс|фунт|злот|юан|тенге|uah|usd|eur|euro|pln|zł|[$€₴])",
    re.IGNORECASE,
)
MONEY_IN_REPLY = (SYMBOL_MONEY, WORD_MONEY)
# Факты товара, которые обязаны совпадать с базой до последней цифры.
FACT_KEYS = ("brand", "name", "price", "total_stock", "available_sizes", "color", "use_case", "features", "variants", "base_currency")
# Текстовые пояснения и подписи ответа — не факты, а их представление.
NOT_A_FACT = {"match_reasons"}


def reply_amounts(reply: str) -> set[float]:
    """Суммы, названные в тексте ответа, в любой записи: символом валюты или словом."""
    text = reply or ""
    return {float(value) for pattern in MONEY_IN_REPLY for value in pattern.findall(text)}


def allowed_amounts(products: list[dict], filters: dict | None = None) -> set[float]:
    """Суммы, которые ответ имеет право называть: цены показанных товаров и границы бюджета."""
    amounts = {float(product.get("price") or 0) for product in products}
    size = (filters or {}).get("size")
    for product in products:
        if size is None:
            continue
        source = product.get("base_currency") or product.get("currency", "USD")
        target = product.get("currency", source)
        for variant in product.get("variants", []):
            if variant.get("size") != size or variant.get("price") is None:
                continue
            amount = convert_amount(float(variant["price"]), source, target,
                                    get_rates().get("rates") if source != target else None)
            if amount is not None:
                amounts.add(float(amount))
    for key in ("min_price", "max_price"):
        value = (filters or {}).get(key)
        if value is not None:
            amounts.add(float(value))
    return amounts


def verify_reply(reply: str, products: list[dict], filters: dict | None = None) -> list[str]:
    """Суммы в ответе, которые нельзя подтвердить ни товаром, ни условием запроса."""
    allowed = allowed_amounts(products, filters)
    return [
        f"неподтверждённая сумма ${amount:g}"
        for amount in sorted(reply_amounts(reply))
        if amount not in allowed
    ]


def verify_products(db: Session, products: list[dict]) -> list[str]:
    """Расхождения между показанными товарами и их текущим состоянием в базе.

    Возвращает пустой список, если всё, что видит пользователь, действительно лежит в SQLite.
    """
    problems: list[str] = []
    for shown in products:
        stored = get_product(db, shown["id"])
        if stored is None:
            problems.append(f"товар {shown['id']} отсутствует в базе")
            continue
        fresh = serialize_product(stored, shown.get("currency"))
        for key in FACT_KEYS:
            if key in NOT_A_FACT:
                continue
            if shown.get(key) != fresh.get(key):
                problems.append(f"товар {shown['id']}: {key} {shown.get(key)!r} != {fresh.get(key)!r}")
    return problems


# Как ответ называет ослабленное условие: если товар условию не отвечает, это обязано быть
# сказано. Исключения в этот список не входят — они не ослабляются никогда.
RELAXATION_MARKERS = (
    "без ограничен", "без обмеж", "другого бренда", "іншого бренду", "другой бренд", "інший бренд",
    # Новые формулировки ослабления: «бренд, цвет и цену не учитываю» (ru/uk).
    "не учитываю", "не враховую",
    "других цвет", "інших кольор", "ближайш", "найближч", "выше бюджета", "вище бюджету",
)


def verify_against_filters(product: dict, filters: dict, reply: str = "") -> list[str]:
    """Противоречит ли показанный товар условиям, о которых говорит ответ.

    Проверка не заменяет SQL: она ловит случай, когда товар попал в выдачу вопреки условиям.
    Ослабление бренда допускается, но только названное вслух; исключения не ослабляются.
    """
    problems: list[str] = []
    identifier = product.get("id")
    brand = str(product.get("brand") or "").lower()
    for excluded in filters.get("exclude_brand") or []:
        if brand == str(excluded).lower():
            problems.append(f"товар {identifier}: бренд {product.get('brand')!r} исключён условием")
    for code, values in (filters.get("exclude_attributes") or {}).items():
        if code == "size":
            # Размер товара лежит в списке доступных размеров, а не отдельным ключом.
            items = values if isinstance(values, (list, tuple)) else [values]
            available = {float(one) for one in product.get("available_sizes") or []}
            if any(float(one) in available for one in items):
                problems.append(f"товар {identifier}: размер исключён условием")
            continue
        actual = product.get({"purpose": "use_case", "tags": "features"}.get(code, code))
        items = values if isinstance(values, (list, tuple)) else [values]
        if isinstance(actual, list):
            if any(str(item).lower() in {str(one).lower() for one in actual} for item in items):
                problems.append(f"товар {identifier}: {code} исключён условием")
        elif actual is not None and any(str(actual).lower() == str(item).lower() for item in items):
            problems.append(f"товар {identifier}: {code} исключён условием")
    wanted = filters.get("brand")
    if wanted and brand != str(wanted).lower():
        relaxed = any(marker in (reply or "").lower() for marker in RELAXATION_MARKERS)
        if not relaxed:
            problems.append(f"товар {identifier}: бренд {product.get('brand')!r} не соответствует условию {wanted!r}")
    return problems


def verify_reasons(products: list[dict]) -> list[str]:
    """У каждого показанного товара должно быть хотя бы одно подтверждённое пояснение."""
    return [
        f"товар {product['id']} без пояснения"
        for product in products
        if not product.get("match_reasons")
    ]
