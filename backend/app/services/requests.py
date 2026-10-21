from sqlalchemy.orm import Session

from sqlalchemy import select

from ..catalog import as_number, variant_axis_value
from ..catalog_rules import SNEAKER_CONFIG
from ..models import ContactRequest, Product, Variant
from ..schemas import ContactRequestCreate


def create_contact_request(db: Session, payload: ContactRequestCreate) -> ContactRequest | None:
    variant = db.get(Variant, payload.variant_id)
    if variant is None or variant.product_id != payload.product_id or not variant.product.active or variant.stock <= 0:
        return None
    request = ContactRequest(
        product_id=payload.product_id,
        variant_id=payload.variant_id,
        name=payload.name.strip(),
        contact=payload.contact.strip(),
        price=variant.price,
        currency=variant.product.currency or "USD",
        comment=payload.comment.strip() if payload.comment else None,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def list_contact_requests(db: Session) -> list[dict]:
    # Внешнее соединение: заявка, ссылка которой не восстановилась после переноса каталога,
    # должна остаться видимой с пустыми полями товара, а не исчезнуть из списка молча.
    rows = db.execute(
        select(ContactRequest, Product, Variant)
        .join(Product, ContactRequest.product_id == Product.id, isouter=True)
        .join(Variant, ContactRequest.variant_id == Variant.id, isouter=True)
        .order_by(ContactRequest.created_at.desc())
    ).all()
    axis = SNEAKER_CONFIG.primary_axis
    return [{
        "id": request.id,
        "created_at": request.created_at.isoformat(timespec="minutes") if request.created_at else "",
        "name": request.name, "contact": request.contact,
        "product": f"{product.brand} {product.name}" if product is not None else "",
        "size": as_number(variant_axis_value(variant, axis)) if variant is not None else None,
        "price": float(request.price), "currency": request.currency or "USD",
        "comment": request.comment, "status": request.status,
    } for request, product, variant in rows]


def update_contact_request_status(db: Session, request_id: int, status: str) -> ContactRequest | None:
    request = db.get(ContactRequest, request_id)
    if request is None:
        return None
    request.status = status
    db.commit()
    db.refresh(request)
    return request


def delete_contact_request(db: Session, request_id: int) -> bool:
    request = db.get(ContactRequest, request_id)
    if request is None:
        return False
    db.delete(request)
    db.commit()
    return True
