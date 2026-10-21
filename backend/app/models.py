"""Модели универсального каталога.

Товар описывается общими полями, а всё, что зависит от типа товара (размер, цвет,
назначение, объём памяти и т.д.), хранится как значения атрибутов. Поэтому добавление
новой категории не требует правки моделей и запросов.
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Attribute(Base):
    """Описание атрибута каталога: код, имя и тип значения."""

    __tablename__ = "attributes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    type: Mapped[str] = mapped_column(String(16), default="enum")  # enum | number | string | boolean


class Product(Base):
    """Товар каталога. Атрибуты товара хранятся отдельно и не зависят от категории."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    brand: Mapped[str] = mapped_column(String(50), index=True)
    category: Mapped[str] = mapped_column(String(60), index=True, default="Other")
    description: Mapped[str] = mapped_column(Text, default="")
    base_price: Mapped[float] = mapped_column(Numeric(10, 2), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    image: Mapped[str] = mapped_column(String(60), default="neutral")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    admin_status: Mapped[str] = mapped_column(String(16), default="published", index=True)
    attributes: Mapped[list["ProductAttribute"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    variants: Mapped[list["Variant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    photos: Mapped[list["ProductImage"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductImage.position"
    )


class ProductImage(Base):
    """Uploaded product photo. Files are retained when a photo is removed from a gallery."""

    __tablename__ = "product_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    filename: Mapped[str] = mapped_column(String(80), unique=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    crop_scale: Mapped[float] = mapped_column(default=1.0)
    crop_x: Mapped[float] = mapped_column(default=0.0)
    crop_y: Mapped[float] = mapped_column(default=0.0)
    product: Mapped[Product] = relationship(back_populates="photos")


class ProductAttribute(Base):
    """Значение атрибута на уровне товара: цвет, назначение, объём памяти и т.п."""

    __tablename__ = "product_attributes"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    attribute_id: Mapped[int] = mapped_column(ForeignKey("attributes.id"), index=True)
    value: Mapped[str] = mapped_column(String(120), index=True)
    product: Mapped[Product] = relationship(back_populates="attributes")
    attribute: Mapped[Attribute] = relationship()


class Variant(Base):
    """Вариант товара: конкретная комплектация со своей ценой и остатком."""

    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(String(60), default="")
    price: Mapped[float] = mapped_column(Numeric(10, 2))
    stock: Mapped[int] = mapped_column(Integer, default=0)
    product: Mapped[Product] = relationship(back_populates="variants")
    attributes: Mapped[list["VariantAttribute"]] = relationship(
        back_populates="variant", cascade="all, delete-orphan"
    )


class VariantAttribute(Base):
    """Значение атрибута на уровне варианта: размер конкретной пары, объём памяти версии."""

    __tablename__ = "variant_attributes"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), index=True)
    attribute_id: Mapped[int] = mapped_column(ForeignKey("attributes.id"), index=True)
    value: Mapped[str] = mapped_column(String(120), index=True)
    variant: Mapped[Variant] = relationship(back_populates="attributes")
    attribute: Mapped[Attribute] = relationship()


# Прежнее имя модели сохранено для совместимости с уже написанным кодом и тестами.
ProductVariant = Variant


class ContactRequest(Base):
    __tablename__ = "contact_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    contact: Mapped[str] = mapped_column(String(150))
    price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class ChatLog(Base):
    __tablename__ = "chat_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(120), index=True)
    language: Mapped[str] = mapped_column(String(8), default="ru", index=True)
    user_message: Mapped[str] = mapped_column(Text)
    assistant_reply: Mapped[str] = mapped_column(Text)
    product_count: Mapped[int] = mapped_column(Integer, default=0)
    product_names: Mapped[str] = mapped_column(Text, default="")
    needs_clarification: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="fallback", index=True)
    filters_json: Mapped[str] = mapped_column(Text, default="{}")
    suggestions_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC), index=True)


class ChatSession(Base):
    """Продолжаемое состояние диалога; товарные факты повторно читаются из каталога."""

    __tablename__ = "chat_sessions"

    session_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    state_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), index=True
    )
