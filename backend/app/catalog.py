"""Универсальное ядро каталога: атрибуты, условия и безопасная выборка.

Ядро не знает, что именно продаёт каталог. Оно умеет хранить у товара и у варианта
произвольные атрибуты и выполнять выборку по нормализованным условиям. Ни одной проверки
вида `if code == "..."` здесь нет: всё, что зависит от типа товара, описывается
конфигурацией (`CatalogConfig`) и данными, а не кодом ядра.

Модель обработки условий:

* `AttributeSpec` — объявление атрибута (код, имя, тип, где хранится);
* `CatalogConfig` — конфигурация каталога: набор атрибутов и перевод старых ключей;
* `ProductQuery` — нормализованные условия (бренд, категория, атрибуты, цена);
* `search_products()` — безопасная выборка: значения подставляются параметрами,
  а не собираются в SQL-строку.
"""

from dataclasses import dataclass, field

from sqlalchemy import Numeric, Select, and_, func, or_, select, true
from sqlalchemy.orm import Session, selectinload

from .models import Attribute, Product, ProductAttribute, Variant, VariantAttribute


@dataclass(frozen=True)
class AttributeSpec:
    """Объявление атрибута в конфигурации каталога."""

    code: str
    name: str
    type: str = "enum"  # enum | number | string | boolean
    scope: str = "product"  # product | variant
    multi: bool = False
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class CatalogConfig:
    """Конфигурация каталога. Всё специфичное для категории товаров живёт здесь."""

    code: str
    name: str
    category: str
    attributes: tuple[AttributeSpec, ...] = ()
    legacy_keys: dict[str, str] = field(default_factory=dict)
    relax_order: tuple[str, ...] = ()
    primary_axis: str | None = None

    def spec(self, code: str) -> AttributeSpec | None:
        for item in self.attributes:
            if item.code == code:
                return item
        return None

    def attribute_code(self, key: str) -> str:
        """Приводит старый ключ фильтра к коду атрибута; неизвестный ключ уже код."""
        return self.legacy_keys.get(key, key)


@dataclass
class ProductQuery:
    """Нормализованные условия поиска. Ничего из схемы БД в них не просачивается."""

    brand: str | None = None
    category: str | None = None
    attributes: dict[str, str | list[str]] = field(default_factory=dict)
    min_price: float | None = None
    max_price: float | None = None
    currency: str | None = None
    exclude_ids: frozenset[int] = frozenset()
    # Исключающие условия: «не Nike», «только не белые». Это тоже условия пользователя,
    # поэтому ядро умеет их выражать, а не только сужать выборку положительными фильтрами.
    exclude_brand: frozenset[str] = frozenset()
    exclude_attributes: dict[str, str | list[str]] = field(default_factory=dict)
    name_query: str | None = None
    sku_query: str | None = None

    def narrowed(self, **changes) -> "ProductQuery":
        data = {
            "brand": self.brand,
            "category": self.category,
            "attributes": dict(self.attributes),
            "min_price": self.min_price,
            "max_price": self.max_price,
            "currency": self.currency,
            "exclude_ids": self.exclude_ids,
            "exclude_brand": self.exclude_brand,
            "exclude_attributes": dict(self.exclude_attributes),
            "name_query": self.name_query,
            "sku_query": self.sku_query,
        }
        data.update(changes)
        return ProductQuery(**data)


def _compare(column, value: str, spec: AttributeSpec | None):
    """Сравнение значения атрибута: числовые типы сравниваются как числа."""
    if spec is not None and spec.type == "number":
        try:
            number = float(str(value).replace(",", "."))
        except ValueError:
            pass
        else:
            return func.cast(column, Numeric) == number
    return func.lower(column) == str(value).strip().lower()


def _escape_like(value: str) -> str:
    """Экранирует спецсимволы LIKE: иначе «%» и «_» работают как шаблоны."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _attribute_clause(code: str, value, spec: AttributeSpec | None):
    """Условие «атрибут есть у товара или у любого его варианта в наличии».

    Значение подставляется параметром через SQLAlchemy, поэтому содержимое фильтра
    не может попасть в SQL как код. Список значений означает «все эти значения сразу»:
    так выражаются многозначные атрибуты (например, товар и для школы, и на осень).
    """
    if isinstance(value, (list, tuple, set)):
        return and_(*[_attribute_clause(code, item, spec) for item in value])
    on_product = Product.attributes.any(
        and_(
            ProductAttribute.attribute.has(Attribute.code == code),
            _compare(ProductAttribute.value, value, spec),
        )
    )
    on_variant = Product.variants.any(
        and_(
            Variant.stock > 0,
            Variant.attributes.any(
                and_(
                    VariantAttribute.attribute.has(Attribute.code == code),
                    _compare(VariantAttribute.value, value, spec),
                )
            ),
        )
    )
    return or_(on_product, on_variant)


def _exclusion_clause(code: str, value, spec: AttributeSpec | None):
    """Условие «атрибута с таким значением у товара нет».

    Исключение проверяется и у товара, и у его вариантов в наличии: если значение есть у
    любого варианта, товар из выдачи уходит. Список значений означает «ни одного из них»,
    поэтому исключения соединяются через `and_`.
    """
    if isinstance(value, (list, tuple, set)):
        items = [item for item in value if str(item).strip()]
        if not items:
            return true()
        return and_(*[_exclusion_clause(code, item, spec) for item in items])
    on_product = Product.attributes.any(
        and_(
            ProductAttribute.attribute.has(Attribute.code == code),
            _compare(ProductAttribute.value, value, spec),
        )
    )
    on_variant = Product.variants.any(
        and_(
            Variant.stock > 0,
            Variant.attributes.any(
                and_(
                    VariantAttribute.attribute.has(Attribute.code == code),
                    _compare(VariantAttribute.value, value, spec),
                )
            ),
        )
    )
    return ~or_(on_product, on_variant)


def base_query() -> Select:
    """Запрос товара вместе с атрибутами и вариантами (без N+1)."""
    return select(Product).options(
        selectinload(Product.attributes).selectinload(ProductAttribute.attribute),
        selectinload(Product.variants)
        .selectinload(Variant.attributes)
        .selectinload(VariantAttribute.attribute),
    )


def search_products(db: Session, query: ProductQuery, config: CatalogConfig | None = None) -> list[Product]:
    """Выборка товаров по нормализованным условиям.

    Неизвестный атрибут не является ошибкой: товаров с таким условием просто не найдётся,
    и это честный ответ вместо выдуманного товара.
    """
    statement = base_query().where(Product.active.is_(True))
    if query.brand:
        statement = statement.where(func.lower(Product.brand) == query.brand.strip().lower())
    if query.category:
        statement = statement.where(func.lower(Product.category) == query.category.strip().lower())
    price_currency = query.currency or ("USD" if query.min_price is not None or query.max_price is not None else None)
    if price_currency:
        statement = statement.where(Product.currency == price_currency)
    if query.min_price is not None:
        statement = statement.where(Product.base_price >= query.min_price)
    if query.max_price is not None:
        statement = statement.where(Product.base_price <= query.max_price)
    if query.exclude_ids:
        statement = statement.where(Product.id.not_in(tuple(query.exclude_ids)))
    if query.exclude_brand:
        # coalesce: пустой бренд не должен выпадать из-за NULL-сравнения.
        statement = statement.where(
            func.coalesce(func.lower(Product.brand), "").not_in([item.strip().lower() for item in query.exclude_brand])
        )
    if query.name_query:
        statement = statement.where(
            func.lower(Product.name).like(f"%{_escape_like(query.name_query.strip().lower())}%", escape="\\")
        )
    if query.sku_query:
        statement = statement.where(
            Product.variants.any(func.lower(Variant.sku) == query.sku_query.strip().lower())
        )
    for code, value in query.attributes.items():
        spec = config.spec(code) if config else None
        statement = statement.where(_attribute_clause(code, value, spec))
    for code, value in query.exclude_attributes.items():
        spec = config.spec(code) if config else None
        statement = statement.where(_exclusion_clause(code, value, spec))
    return list(db.scalars(statement.order_by(Product.id)).unique().all())


def get_product(db: Session, product_id: int) -> Product | None:
    return db.scalar(base_query().where(Product.id == product_id))


def group_attribute_rows(rows) -> dict:
    """Собирает строки атрибутов в словарь: одно значение — скаляр, несколько — список.

    Многозначные атрибуты (например, несколько особенностей у товара) не теряются.
    """
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row.attribute.code, []).append(row.value)
    return {code: values[0] if len(values) == 1 else values for code, values in grouped.items()}


def serialize_product(product: Product) -> dict:
    """Универсальный вид товара: общие поля, словарь атрибутов и варианты с их атрибутами."""
    variants = [
        {
            "id": variant.id,
            "sku": variant.sku,
            "price": float(variant.price),
            "stock": variant.stock,
            "attributes": group_attribute_rows(variant.attributes),
        }
        for variant in product.variants
    ]
    return {
        "id": product.id,
        "name": product.name,
        "brand": product.brand,
        "category": product.category,
        "description": product.description,
        "base_price": float(product.base_price),
        "currency": product.currency or "USD",
        "image": product.image,
        "active": product.active,
        "attributes": group_attribute_rows(product.attributes),
        "variants": variants,
        "total_stock": sum(item["stock"] for item in variants),
    }


def build_query(config: CatalogConfig, conditions: dict) -> ProductQuery:
    """Переводит нормализованные условия приложения в универсальный запрос.

    Бренд, категория, цена и исключения известны ядру. Всё остальное считается атрибутом:
    старые имена ключей переводит конфигурация каталога, а неизвестный ключ трактуется как
    код атрибута — поэтому новая ось не требует правки ядра.
    """
    query = ProductQuery()
    for key, value in conditions.items():
        if value is None or value == "":
            continue
        if key == "brand":
            query.brand = str(value)
        elif key == "currency":
            query.currency = str(value)
        elif key == "category":
            query.category = str(value)
        elif key == "min_price":
            query.min_price = float(value)
        elif key == "max_price":
            query.max_price = float(value)
        elif key == "name_query":
            query.name_query = str(value)
        elif key == "sku_query":
            query.sku_query = str(value).strip()
        elif key == "exclude_ids":
            query.exclude_ids = frozenset(int(item) for item in value)
        elif key == "exclude_brand":
            query.exclude_brand = frozenset(_as_value_list(value))
        elif key == "exclude_attributes" and isinstance(value, dict):
            query.exclude_attributes.update({str(code): _as_attribute_value(item) for code, item in value.items()})
        elif key == "attributes" and isinstance(value, dict):
            query.attributes.update({str(code): _as_attribute_value(item) for code, item in value.items()})
        else:
            query.attributes[config.attribute_code(str(key))] = _as_attribute_value(value)
    return query


def _as_attribute_value(value) -> str | list[str]:
    """Значение атрибута: строка или список строк для многозначных атрибутов."""
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return str(value)


def _as_value_list(value) -> list[str]:
    """Скаляр или список — всегда список непустых строк."""
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]
    return [str(item) for item in items if str(item).strip()]


def declared_attribute_values(db: Session, config: CatalogConfig) -> dict[str, set[str]]:
    """Значения, которые реально есть в каталоге, по каждому объявленному атрибуту.

    Нужно, чтобы решение «это значение каталог знает» принималось по данным, а не по
    списку в коде: тогда второй каталог проверяется теми же правилами без правок.
    """
    codes = [spec.code for spec in config.attributes]
    values: dict[str, set[str]] = {code: set() for code in codes}
    if not codes:
        return values
    for code, value in db.execute(
        select(Attribute.code, ProductAttribute.value)
        .join(Attribute, ProductAttribute.attribute_id == Attribute.id)
        .where(Attribute.code.in_(codes))
    ):
        values.setdefault(str(code), set()).add(str(value))
    for code, value in db.execute(
        select(Attribute.code, VariantAttribute.value)
        .join(Attribute, VariantAttribute.attribute_id == Attribute.id)
        .where(Attribute.code.in_(codes))
    ):
        values.setdefault(str(code), set()).add(str(value))
    return values


def catalogue_brands(db: Session) -> set[str]:
    """Бренды, которые реально есть в каталоге."""
    return {str(brand) for brand in db.scalars(select(Product.brand).where(Product.active.is_(True)).distinct()) if brand}


def sync_attributes(db: Session, config: CatalogConfig) -> dict[str, Attribute]:
    """Приводит справочник атрибутов в БД к конфигурации каталога."""
    existing = {attribute.code: attribute for attribute in db.scalars(select(Attribute)).all()}
    for spec in config.attributes:
        attribute = existing.get(spec.code)
        if attribute is None:
            attribute = Attribute(code=spec.code, name=spec.name, type=spec.type)
            db.add(attribute)
            existing[spec.code] = attribute
        else:
            attribute.name, attribute.type = spec.name, spec.type
    db.flush()
    return existing


def as_number(value: object) -> float | None:
    """Числовое значение атрибута или None, если значение числом не является."""
    if value is None or isinstance(value, (list, tuple, set)):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def variant_axis_value(variant: Variant, axis: str | None) -> str | None:
    """Значение оси варианта.

    Нужно там, где вариант надо узнать по его оси: в сиде, в карточке заявки и в совместимом
    виде товара. Ядро не знает, что это за ось: код оси приходит из конфигурации каталога.
    """
    if axis is None:
        return None
    for item in variant.attributes:
        if item.attribute.code == axis:
            return item.value
    return None


def set_attribute_values(owner, values: dict, attributes_by_code: dict[str, Attribute]) -> None:
    """Записывает значения атрибутов товару или варианту.

    Работает и для товара, и для варианта: класс строки берётся из связи, поэтому дублировать
    логику под каждый тип владельца не нужно. Значение-список даёт несколько строк — так
    описываются многозначные атрибуты.
    """
    row_class = type(owner).attributes.property.mapper.class_
    rows = []
    for code, value in values.items():
        attribute = attributes_by_code.get(code)
        if attribute is None:
            continue
        for item in value if isinstance(value, (list, tuple, set)) else [value]:
            rows.append(row_class(attribute=attribute, value=str(item)))
    owner.attributes = rows
