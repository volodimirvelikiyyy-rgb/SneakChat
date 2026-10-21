"""Перенос данных при переходе на универсальный каталог.

Таблицы каталога пересоздаются из сида, поэтому идентификаторы товаров и вариантов меняются.
Чтобы сохранённые заявки не начали ссылаться на чужие товары, их ссылки восстанавливаются
по паре «бренд + название» и по значению оси варианта, снятым до пересоздания.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import as_number, variant_axis_value
from .catalog_rules import SNEAKER_CONFIG
from .models import ContactRequest, Product


def _same_axis(left: object, right: object) -> bool:
    """Сравнение значений оси: сначала как строки, затем как числа.

    Ось варианта не обязана быть числовой, поэтому сравнивать её через float нельзя:
    значение вроде «XL» обрушило бы запуск приложения.
    """
    if left is None or right is None:
        return False
    if str(left).strip().lower() == str(right).strip().lower():
        return True
    left_number, right_number = as_number(left), as_number(right)
    return left_number is not None and left_number == right_number


def relink_contact_requests(db: Session, links: list[dict]) -> dict:
    """Возвращает заявкам ссылки на товар и вариант после пересоздания каталога.

    Возвращает счётчики: сколько заявок восстановлено и сколько обнулено. Обнуление нужно,
    когда товара в новом каталоге нет: оставлять прежний идентификатор нельзя, потому что
    его займёт другой товар и заявка начнёт показывать чужую модель.
    """
    axis = SNEAKER_CONFIG.primary_axis
    restored, cleared = 0, 0
    for link in links:
        request = db.get(ContactRequest, link.get("request_id"))
        if request is None:
            continue
        product = db.scalar(
            select(Product)
            .where(Product.brand == link.get("brand"), Product.name == link.get("name"))
            .order_by(Product.id)
        )
        if product is None:
            request.product_id, request.variant_id = 0, 0
            cleared += 1
            continue
        request.product_id = product.id
        variant = next(
            (
                item
                for item in product.variants
                if _same_axis(variant_axis_value(item, axis), link.get("size"))
            ),
            None,
        )
        request.variant_id = variant.id if variant is not None else 0
        restored += 1
    if restored or cleared:
        db.commit()
    return {"restored": restored, "cleared": cleared}
