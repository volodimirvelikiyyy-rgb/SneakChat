from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..catalog_rules import MAX_SIZE, MIN_SIZE
from ..database import get_db
from ..schemas import ProductRead
from ..services.products import find_products, get_product, serialize_product

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("/search", response_model=list[ProductRead])
def search_products(
    brand: str | None = None,
    color: str | None = None,
    size: float | None = Query(default=None, ge=MIN_SIZE, le=MAX_SIZE),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    currency: Literal["USD", "EUR", "UAH"] | None = None,
    display_currency: Literal["USD", "EUR", "UAH"] | None = None,
    gender: str | None = None,
    use_case: str | None = None,
    feature: str | None = None,
    attribute: list[str] | None = Query(default=None, description="Произвольный атрибут в виде код:значение, можно несколько раз"),
    db: Session = Depends(get_db),
):
    """Поиск по совместимым параметрам и по любым атрибутам каталога.

    Параметры brand/color/size/gender/use_case/feature описывают кроссовки, а `attribute`
    работает для любого каталога: `?attribute=storage:256GB&attribute=color:Black`.
    """
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(status_code=422, detail="min_price must not exceed max_price")
    attributes = _parse_attributes(attribute)
    return [
        serialize_product(product, display_currency)
        for product in find_products(
            db, brand, color, size, min_price, max_price, gender, use_case, feature, attributes,
            currency=currency or ("USD" if (min_price is not None or max_price is not None) and not display_currency else None),
            display_currency=display_currency,
        )
    ]


def _parse_attributes(items: list[str] | None) -> dict[str, str]:
    """Разбирает повторяющийся параметр `attribute` вида код:значение."""
    attributes: dict[str, str] = {}
    for item in items or []:
        code, separator, value = item.partition(":")
        if separator and code.strip() and value.strip():
            attributes[code.strip()] = value.strip()
    return attributes


@router.get("", response_model=list[ProductRead])
def list_products(display_currency: Literal["USD", "EUR", "UAH"] | None = None, db: Session = Depends(get_db)):
    return [serialize_product(product, display_currency) for product in find_products(db)]


@router.get("/{product_id}", response_model=ProductRead)
def read_product(product_id: int, display_currency: Literal["USD", "EUR", "UAH"] | None = None, db: Session = Depends(get_db)):
    product = get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return serialize_product(product, display_currency)
