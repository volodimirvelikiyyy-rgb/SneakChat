"""Owner-only local product administration endpoints."""

from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import catalog
from ..admin_auth import COOKIE_NAME, SESSION_SECONDS, issue_session, read_auth, require_admin, require_local_request, verify_password
from ..catalog_rules import SNEAKER_CONFIG
from ..database import get_db
from ..models import Attribute, Product, ProductAttribute, ProductImage, Variant, VariantAttribute
from ..services.products import serialize_product

router = APIRouter(prefix="/api/admin", tags=["local admin"])
UPLOAD_DIR = Path(__file__).resolve().parents[3] / "frontend" / "public" / "images" / "sneakers" / "uploads"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_failed_logins: dict[str, tuple[int, float]] = {}
_RESERVED_ATTRIBUTES = {"color", "gender", "purpose", "tags"}
ProductStatus = Literal["published", "hidden", "draft", "deleted"]
Currency = Literal["USD", "EUR", "UAH"]


class LoginPayload(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class VariantInput(BaseModel):
    size: float = Field(ge=34, le=50)
    stock: int = Field(ge=0, le=999)


class CharacteristicInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=120)


class ProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brand: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=120)
    color: str = Field(min_length=1, max_length=120)
    price: float = Field(ge=0, le=10000)
    currency: Currency = "USD"
    description: str = Field(default="", max_length=4000)
    gender: str = Field(default="", max_length=120)
    purpose: str = Field(default="", max_length=120)
    features: list[str] = Field(default_factory=list, max_length=20)
    characteristics: list[CharacteristicInput] = Field(default_factory=list, max_length=40)
    variants: list[VariantInput] = Field(default_factory=list, max_length=40)
    status: ProductStatus = "draft"

    @field_validator("brand", "name", "color", mode="before")
    @classmethod
    def trim_required(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def unique_sizes(self) -> "ProductInput":
        sizes = [item.size for item in self.variants]
        if len(sizes) != len(set(sizes)):
            raise ValueError("Размеры не должны повторяться")
        return self


class StatusInput(BaseModel):
    status: ProductStatus


class PhotoOrderInput(BaseModel):
    photo_ids: list[int] = Field(min_length=1, max_length=40)


class PhotoFrameInput(BaseModel):
    is_primary: bool | None = None
    crop_scale: float | None = Field(default=None, ge=1.0, le=2.5)
    crop_x: float | None = Field(default=None, ge=-50, le=50)
    crop_y: float | None = Field(default=None, ge=-50, le=50)


def _owner_host_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/me", dependencies=[])
def admin_me(request: Request):
    require_local_request(request)
    config = read_auth()
    if config is None:
        return {"configured": False, "authenticated": False}
    from ..admin_auth import valid_session

    return {"configured": True, "authenticated": valid_session(request.cookies.get(COOKIE_NAME), config)}


@router.post("/login", dependencies=[])
def admin_login(payload: LoginPayload, request: Request, response: Response):
    require_local_request(request)
    key = _owner_host_key(request)
    attempts, blocked_until = _failed_logins.get(key, (0, 0.0))
    if blocked_until > time.time():
        raise HTTPException(status_code=429, detail="Слишком много попыток. Подождите минуту.")
    config = read_auth()
    if config is None:
        raise HTTPException(status_code=503, detail="Настройте пароль владельца командой python -m app.admin_setup")
    if not verify_password(payload.password, config):
        attempts += 1
        _failed_logins[key] = (0, time.time() + 60) if attempts >= 5 else (attempts, 0.0)
        raise HTTPException(status_code=401, detail="Неверный пароль")
    _failed_logins.pop(key, None)
    response.set_cookie(
        COOKIE_NAME, issue_session(config), max_age=SESSION_SECONDS, httponly=True,
        secure=request.url.scheme == "https", samesite="strict", path="/",
    )
    return {"ok": True}


@router.post("/logout")
def admin_logout(response: Response, _: dict = Depends(require_admin)):
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
    return {"ok": True}


def _photo_data(photo: ProductImage) -> dict:
    return {
        "id": photo.id,
        "url": f"/images/sneakers/uploads/{photo.filename}",
        "filename": photo.filename,
        "position": photo.position,
        "is_primary": photo.is_primary,
        "crop_scale": photo.crop_scale,
        "crop_x": photo.crop_x,
        "crop_y": photo.crop_y,
    }


def _read_product(product: Product) -> dict:
    data = serialize_product(product)
    attributes = data.get("attributes", {})
    data.update({
        "status": product.admin_status,
        "color": attributes.get("color") or "",
        "gender": attributes.get("gender") or "",
        "purpose": attributes.get("purpose") or "",
        "features": attributes.get("tags") if isinstance(attributes.get("tags"), list) else ([attributes["tags"]] if attributes.get("tags") else []),
        "characteristics": [
            {"code": row.attribute.code, "name": row.attribute.name, "value": row.value}
            for row in product.attributes if row.attribute.code not in _RESERVED_ATTRIBUTES
        ],
        "photos": [_photo_data(photo) for photo in sorted(product.photos, key=lambda item: (item.position, item.id)) if photo.is_active],
        "has_managed_photos": bool(product.photos),
    })
    return data


def _query_products(db: Session, brand: str, model: str, status: str, stock: str) -> list[Product]:
    statement = select(Product).options(
        selectinload(Product.attributes).selectinload(ProductAttribute.attribute),
        selectinload(Product.variants).selectinload(Variant.attributes).selectinload(VariantAttribute.attribute),
        selectinload(Product.photos),
    )
    if brand:
        statement = statement.where(Product.brand.ilike(f"%{brand.strip()}%"))
    if model:
        statement = statement.where(Product.name.ilike(f"%{model.strip()}%"))
    if status in {"published", "hidden", "draft", "deleted"}:
        statement = statement.where(Product.admin_status == status)
    products = list(db.scalars(statement.order_by(Product.id)).unique().all())
    if stock == "available":
        return [product for product in products if any(variant.stock > 0 for variant in product.variants)]
    if stock == "sold-out":
        return [product for product in products if not any(variant.stock > 0 for variant in product.variants)]
    return products


@router.get("/products", dependencies=[Depends(require_admin)])
def admin_products(
    brand: str = Query(default="", max_length=80), model: str = Query(default="", max_length=120),
    status: str = Query(default="", max_length=20), stock: str = Query(default="", max_length=20),
    db: Session = Depends(get_db),
):
    return [_read_product(product) for product in _query_products(db, brand, model, status, stock)]


@router.get("/products/{product_id}", dependencies=[Depends(require_admin)])
def admin_product(product_id: int, db: Session = Depends(get_db)):
    product = db.scalar(select(Product).options(
        selectinload(Product.attributes).selectinload(ProductAttribute.attribute),
        selectinload(Product.variants).selectinload(Variant.attributes).selectinload(VariantAttribute.attribute),
        selectinload(Product.photos),
    ).where(Product.id == product_id))
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return _read_product(product)


def _set_attribute_values(db: Session, product: Product, payload: ProductInput) -> None:
    values: list[tuple[str, str, str]] = [
        ("color", "Цвет", payload.color),
        ("gender", "Пол", payload.gender),
        ("purpose", "Назначение", payload.purpose),
    ]
    values.extend(("tags", "Особенности", item.strip()) for item in payload.features if item.strip())
    for item in payload.characteristics:
        label = item.name.strip()
        value = item.value.strip()
        if not label or not value:
            continue
        code = "custom_" + hashlib.sha256(label.casefold().encode("utf-8")).hexdigest()[:16]
        values.append((code, label, value))

    for row in list(product.attributes):
        db.delete(row)
    db.flush()
    for code, label, value in values:
        attribute = db.scalar(select(Attribute).where(Attribute.code == code))
        if attribute is None:
            attribute = Attribute(code=code, name=label, type="enum")
            db.add(attribute)
            db.flush()
        db.add(ProductAttribute(product_id=product.id, attribute_id=attribute.id, value=value))


def _sync_variants(db: Session, product: Product, payload: ProductInput) -> None:
    axis = SNEAKER_CONFIG.primary_axis
    existing: dict[float, Variant] = {}
    for variant in product.variants:
        size = catalog.as_number(catalog.variant_axis_value(variant, axis))
        if size is not None:
            existing[size] = variant
    submitted = {item.size for item in payload.variants}
    for size, variant in existing.items():
        if size not in submitted:
            variant.stock = 0
            variant.price = payload.price
    for item in payload.variants:
        variant = existing.get(item.size)
        if variant is None:
            variant = Variant(product_id=product.id, sku="SC-" + secrets.token_hex(8).upper(), price=payload.price, stock=item.stock)
            db.add(variant)
            db.flush()
            size_attribute = db.scalar(select(Attribute).where(Attribute.code == axis))
            if size_attribute is None:
                size_attribute = Attribute(code=axis, name="Размер", type="number")
                db.add(size_attribute)
                db.flush()
            db.add(VariantAttribute(variant_id=variant.id, attribute_id=size_attribute.id, value=str(item.size).removesuffix(".0")))
        else:
            variant.stock = item.stock
            variant.price = payload.price


def _save_product(db: Session, product: Product, payload: ProductInput) -> Product:
    product.brand = payload.brand.strip()
    product.name = payload.name.strip()
    product.category = SNEAKER_CONFIG.category
    product.base_price = payload.price
    product.currency = payload.currency
    product.description = payload.description.strip()
    product.admin_status = payload.status
    product.active = payload.status == "published"
    db.flush()
    _set_attribute_values(db, product, payload)
    _sync_variants(db, product, payload)
    db.flush()
    return product


@router.post("/products", status_code=201, dependencies=[Depends(require_admin)])
def create_product(payload: ProductInput, db: Session = Depends(get_db)):
    product = Product(
        brand=payload.brand.strip(), name=payload.name.strip(), category=SNEAKER_CONFIG.category,
        base_price=payload.price, currency=payload.currency, description=payload.description.strip(), image="fallback",
        active=payload.status == "published", admin_status=payload.status,
    )
    db.add(product)
    db.flush()
    _save_product(db, product, payload)
    db.commit()
    product = db.scalar(select(Product).options(
        selectinload(Product.attributes).selectinload(ProductAttribute.attribute),
        selectinload(Product.variants).selectinload(Variant.attributes).selectinload(VariantAttribute.attribute),
        selectinload(Product.photos),
    ).where(Product.id == product.id))
    return _read_product(product)


@router.put("/products/{product_id}", dependencies=[Depends(require_admin)])
def update_product(product_id: int, payload: ProductInput, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    _save_product(db, product, payload)
    db.commit()
    product = db.scalar(select(Product).options(
        selectinload(Product.attributes).selectinload(ProductAttribute.attribute),
        selectinload(Product.variants).selectinload(Variant.attributes).selectinload(VariantAttribute.attribute),
        selectinload(Product.photos),
    ).where(Product.id == product_id))
    return _read_product(product)


@router.patch("/products/{product_id}/status", dependencies=[Depends(require_admin)])
def update_product_status(product_id: int, payload: StatusInput, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    product.admin_status = payload.status
    product.active = payload.status == "published"
    db.commit()
    return {"id": product.id, "status": product.admin_status, "active": product.active}


@router.delete("/products/{product_id}", dependencies=[Depends(require_admin)])
def archive_product(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    product.admin_status = "deleted"
    product.active = False
    db.commit()
    return {"id": product.id, "status": product.admin_status}


def _image_dimensions(data: bytes, content_type: str) -> tuple[int, int]:
    if content_type == "image/jpeg":
        if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            raise HTTPException(status_code=415, detail="Повреждённый JPEG")
        index = 2
        while index + 4 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                return int.from_bytes(data[index + 3:index + 5], "big"), int.from_bytes(data[index + 5:index + 7], "big")
            if marker == 0xDA:
                break
            index += length
        raise HTTPException(status_code=415, detail="Не удалось прочитать JPEG")
    if content_type == "image/png":
        if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR" or b"IEND" not in data[-16:]:
            raise HTTPException(status_code=415, detail="Повреждённый PNG")
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if content_type == "image/webp":
        if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise HTTPException(status_code=415, detail="Повреждённый WebP")
        chunk = data[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
        raise HTTPException(status_code=415, detail="Не удалось прочитать WebP")
    raise HTTPException(status_code=415, detail="Разрешены JPG, PNG и WebP")


@router.post("/products/{product_id}/photos", status_code=201, dependencies=[Depends(require_admin)])
async def upload_photo(product_id: int, request: Request, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Разрешены JPG, PNG и WebP")
    declared_size = int(request.headers.get("content-length", "0") or 0)
    if declared_size > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Максимальный размер фото — 10 МБ")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Максимальный размер фото — 10 МБ")
    data = bytes(body)
    width, height = _image_dimensions(data, content_type)
    if width < 1 or height < 1 or width > 12000 or height > 12000 or width * height > 36_000_000:
        raise HTTPException(status_code=413, detail="Размеры фото превышают допустимый предел")
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(16)}.{extension}"
    destination = (UPLOAD_DIR / filename).resolve()
    if destination.parent != UPLOAD_DIR.resolve():
        raise HTTPException(status_code=400, detail="Недопустимый путь файла")
    try:
        with destination.open("xb") as image_file:
            image_file.write(data)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Повторите загрузку фото") from None
    active_photos = [photo for photo in product.photos if photo.is_active]
    photo = ProductImage(
        product_id=product.id, filename=filename, position=len(active_photos),
        is_primary=not any(item.is_primary for item in active_photos),
    )
    db.add(photo)
    if photo.is_primary:
        product.image = filename.rsplit(".", 1)[0]
    db.commit()
    db.refresh(photo)
    return _photo_data(photo)


class ImportCurrentPhotosInput(BaseModel):
    filenames: list[str] = Field(min_length=1, max_length=12)


@router.post("/products/{product_id}/photos/import-current", status_code=201, dependencies=[Depends(require_admin)])
def import_current_photos(product_id: int, payload: ImportCurrentPhotosInput, db: Session = Depends(get_db)):
    """Copy the visible local catalog photos only after the owner explicitly chooses import."""
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    if product.photos:
        raise HTTPException(status_code=409, detail="У товара уже была настроена галерея")
    source_root = UPLOAD_DIR.parent.resolve()
    copied: list[ProductImage] = []
    for index, filename in enumerate(dict.fromkeys(payload.filenames)):
        name = Path(filename).name
        if name != filename or not name.lower().endswith(".jpg"):
            raise HTTPException(status_code=422, detail="Можно импортировать только JPG из каталога")
        source = (source_root / name).resolve()
        if source.parent != source_root or not source.is_file():
            raise HTTPException(status_code=404, detail="Исходная фотография не найдена")
        data = source.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Исходная фотография превышает 10 МБ")
        _image_dimensions(data, "image/jpeg")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        destination_name = f"{secrets.token_hex(16)}.jpg"
        destination = (UPLOAD_DIR / destination_name).resolve()
        if destination.parent != UPLOAD_DIR.resolve():
            raise HTTPException(status_code=400, detail="Недопустимый путь файла")
        with destination.open("xb") as handle:
            handle.write(data)
        photo = ProductImage(
            product_id=product.id, filename=destination_name, position=index, is_primary=index == 0,
        )
        db.add(photo)
        copied.append(photo)
    if copied:
        product.image = copied[0].filename.rsplit(".", 1)[0]
    db.commit()
    return [_photo_data(photo) for photo in copied]


def _get_active_photo(db: Session, product_id: int, photo_id: int) -> ProductImage:
    photo = db.scalar(select(ProductImage).where(
        ProductImage.id == photo_id, ProductImage.product_id == product_id, ProductImage.is_active.is_(True)
    ))
    if photo is None:
        raise HTTPException(status_code=404, detail="Фото не найдено")
    return photo


@router.patch("/products/{product_id}/photos/{photo_id}", dependencies=[Depends(require_admin)])
def update_photo(product_id: int, photo_id: int, payload: PhotoFrameInput, db: Session = Depends(get_db)):
    photo = _get_active_photo(db, product_id, photo_id)
    if payload.is_primary:
        photos = list(db.scalars(select(ProductImage).where(
            ProductImage.product_id == product_id, ProductImage.is_active.is_(True)
        ).order_by(ProductImage.position, ProductImage.id)).all())
        ordered = [photo] + [item for item in photos if item.id != photo.id]
        for position, item in enumerate(ordered):
            item.position = position
            item.is_primary = position == 0
        product = db.get(Product, product_id)
        product.image = photo.filename.rsplit(".", 1)[0]
    for field in ("crop_scale", "crop_x", "crop_y"):
        value = getattr(payload, field)
        if value is not None:
            setattr(photo, field, value)
    db.commit()
    db.refresh(photo)
    return _photo_data(photo)


@router.put("/products/{product_id}/photos/order", dependencies=[Depends(require_admin)])
def reorder_photos(product_id: int, payload: PhotoOrderInput, db: Session = Depends(get_db)):
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    photos = list(db.scalars(select(ProductImage).where(
        ProductImage.product_id == product_id, ProductImage.is_active.is_(True)
    )).all())
    if len(payload.photo_ids) != len(photos) or set(payload.photo_ids) != {photo.id for photo in photos}:
        raise HTTPException(status_code=422, detail="Передан неполный список фотографий")
    by_id = {photo.id: photo for photo in photos}
    for position, photo_id in enumerate(payload.photo_ids):
        by_id[photo_id].position = position
        by_id[photo_id].is_primary = position == 0
    db.commit()
    return [_photo_data(by_id[photo_id]) for photo_id in payload.photo_ids]


@router.delete("/products/{product_id}/photos/{photo_id}", dependencies=[Depends(require_admin)])
def remove_photo(product_id: int, photo_id: int, db: Session = Depends(get_db)):
    photo = _get_active_photo(db, product_id, photo_id)
    was_primary = photo.is_primary
    photo.is_active = False
    photo.is_primary = False
    active = list(db.scalars(select(ProductImage).where(
        ProductImage.product_id == product_id, ProductImage.is_active.is_(True), ProductImage.id != photo_id
    ).order_by(ProductImage.position, ProductImage.id)).all())
    if was_primary and active:
        active[0].is_primary = True
        product = db.get(Product, product_id)
        product.image = active[0].filename.rsplit(".", 1)[0]
    for position, item in enumerate(active):
        item.position = position
    db.commit()
    return {"ok": True, "original_retained": True}
