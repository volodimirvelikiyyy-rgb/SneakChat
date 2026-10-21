import re

from sqlalchemy.orm import Session

from ..catalog import as_number, variant_axis_value
from ..catalog_rules import SNEAKER_CONFIG
from ..services.products import find_products, get_product, serialize_product


def search_products(db: Session, **filters) -> list[dict]:
    """Поиск по нормализованным условиям. Ядро выборки — `catalog.search_products`."""
    name_query = filters.pop("name_query", None) or ""
    display_currency = filters.get("display_currency")
    products = find_products(db, **filters)
    if name_query:
        # Spaces may vary before vN, but v13 must not match v130.
        pattern = r"(?<!\w)" + r"\s*".join(re.escape(word) for word in name_query.lower().split()) + r"(?!\w)"
        products = [product for product in products if re.search(pattern, product.name.lower())]
    display_currency = display_currency or filters.get("currency")
    return [serialize_product(product, display_currency) for product in products]


def get_product_details(db: Session, product_id: int) -> dict | None:
    product = get_product(db, product_id)
    return serialize_product(product) if product else None


def check_stock(db: Session, product_id: int, size: float) -> bool:
    """Есть ли у товара вариант с такой осью в наличии (для кроссовок ось — размер)."""
    product = get_product(db, product_id)
    if product is None:
        return False
    axis = SNEAKER_CONFIG.primary_axis
    return any(
        as_number(variant_axis_value(variant, axis)) == size and variant.stock > 0
        for variant in product.variants
    )


def recommend_products(db: Session, *, exclude_ids: set[int] | None = None, limit: int = 3, **filters) -> list[dict]:
    products = search_products(db, **filters)
    if exclude_ids:
        products = [product for product in products if product["id"] not in exclude_ids]
    # A general recommendation is not a request for the cheapest model.
    return sorted(products, key=lambda product: product["id"])[:limit]
