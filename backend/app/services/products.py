"""Совместимый слой каталога для текущего приложения.

Универсальный механизм живёт в `catalog.py` и про кроссовки не знает. Здесь остался только
перевод прежних ключей фильтров и прежний вид карточки товара, чтобы фронтенд и сценарии
Этапов 1–2 продолжили работать без переписывания. Когда приложений станет больше, этот слой
переедет в конкретную конфигурацию каталога.
"""

import re

from sqlalchemy.orm import Session

from .. import catalog
from ..catalog_rules import IMAGE_PATH, SNEAKER_CONFIG
from ..models import Product
from .currency import convert_amount, get_rates


def find_products(
    db: Session,
    brand: str | None = None,
    color: str | None = None,
    size: float | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    gender: str | None = None,
    use_case: str | None = None,
    feature: str | None = None,
    attributes: dict | None = None,
    exclude_brand: list | tuple | set | None = None,
    exclude_attributes: dict | None = None,
    currency: str | None = None,
    display_currency: str | None = None,
    sku_query: str | None = None,
) -> list[Product]:
    """Прежний вход поиска: оси каталога переводит в атрибуты его конфигурация."""
    price_currency = currency or display_currency
    conditions = {
        "brand": brand,
        "color": color,
        "size": size,
        "min_price": min_price,
        "max_price": max_price,
        "currency": currency if display_currency is None else None,
        "gender": gender,
        "use_case": use_case,
        "feature": feature,
        "attributes": attributes,
        "exclude_brand": exclude_brand,
        "exclude_attributes": exclude_attributes,
        "sku_query": sku_query,
    }
    if display_currency and (min_price is not None or max_price is not None):
        snapshot = get_rates()
        rates = snapshot.get("rates") if snapshot.get("available") else None
        # Run the non-price predicates in SQL; apply mixed-currency budgets only with rates.
        conditions.update(min_price=None, max_price=None, currency=None if rates else price_currency)
        candidates = catalog.search_products(db, catalog.build_query(SNEAKER_CONFIG, conditions), SNEAKER_CONFIG)
        def within_budget(product: Product) -> bool:
            price = convert_amount(float(product.base_price), product.currency or "USD", price_currency, rates)
            if price is None:
                return False
            return (min_price is None or price >= min_price) and (max_price is None or price <= max_price)
        return [product for product in candidates if within_budget(product)]
    return catalog.search_products(db, catalog.build_query(SNEAKER_CONFIG, conditions), SNEAKER_CONFIG)


def get_product(db: Session, product_id: int) -> Product | None:
    product = catalog.get_product(db, product_id)
    return product if product is not None and product.active else None


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [str(value)]


def model_image_key(brand: object, name: object) -> str:
    """Ключ фото конкретной модели: файл `/images/sneakers/{ключ}.jpg`.

    Карточка обещает конкретную модель, поэтому и фото должно быть её. Ключ строится из бренда и
    названия, а не из цвета: две разные модели одного цвета больше не делят одну картинку.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", f"{brand} {name}".lower()).strip("-")
    return slug or "fallback"


def serialize_product(product: Product, display_currency: str | None = None) -> dict:
    """Прежний вид карточки товара плюс универсальные поля.

    Фронтенд читает brand, name, price, color, gender, use_case, features, image_key,
    available_sizes, total_stock и variants[].size — они сохраняются без изменений.
    """
    data = catalog.serialize_product(product)
    attributes = data["attributes"]
    axis = SNEAKER_CONFIG.primary_axis
    variants = [
        {
            "id": item["id"],
            "sku": item["sku"],
            "price": item["price"],
            "size": catalog.as_number(item["attributes"].get(axis)),
            "stock": item["stock"],
            "attributes": item["attributes"],
        }
        for item in data["variants"]
    ]
    variants.sort(key=lambda item: (item["size"] is None, item["size"]))
    source_currency = data.get("currency", "USD")
    target_currency = display_currency or source_currency
    snapshot = get_rates() if display_currency and display_currency != source_currency else None
    displayed_price = convert_amount(data["base_price"], source_currency, target_currency,
                                     snapshot.get("rates") if snapshot and snapshot.get("available") else None)
    displayed_currency = target_currency if displayed_price is not None else source_currency
    return {
        **data,
        "base_currency": source_currency,
        "color": attributes.get("color"),
        "gender": attributes.get("gender"),
        "use_case": attributes.get("purpose"),
        "features": _as_list(attributes.get("tags")),
        "price": displayed_price if displayed_price is not None else data["base_price"],
        "currency": displayed_currency,
        "image_key": model_image_key(data["brand"], data["name"]),
        "image_url": f"{IMAGE_PATH}/{data['image']}.svg",
        "photos": [
            {
                "id": photo.id,
                "url": f"/images/sneakers/uploads/{photo.filename}",
                "position": photo.position,
                "is_primary": photo.is_primary,
                "crop_scale": photo.crop_scale,
                "crop_x": photo.crop_x,
                "crop_y": photo.crop_y,
            }
            for photo in sorted(product.photos, key=lambda item: (item.position, item.id))
            if photo.is_active
        ],
        "variants": variants,
        "available_sizes": [item["size"] for item in variants if item["size"] is not None and item["stock"] > 0],
    }
