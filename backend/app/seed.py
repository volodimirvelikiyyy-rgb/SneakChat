"""Демо-каталог кроссовок.

Данные каталога описаны как данные: общие поля товара и значения атрибутов. Цвет, пол,
назначение, теги и размер не являются колонками, поэтому тот же механизм подойдёт каталогу
любой другой категории — достаточно своей конфигурации и своего списка.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import set_attribute_values, sync_attributes, variant_axis_value
from .catalog_rules import SNEAKER_CONFIG
from .models import Product, Variant

# brand, name, color, gender, purpose, price, image, tags, [(size, stock), ...]
CATALOG = [
    ("Nike", "Air Max Pulse", "White", "Men", "Walking", 119, "white", "|school|autumn|", [(39, 2), (40, 3), (41, 3), (42, 5), (43, 2), (44, 0)]),
    ("Nike", "Dunk Low Retro", "Black", "Unisex", "Lifestyle", 110, "black", "|school|autumn|", [(35, 2), (36, 3), (37, 2), (38, 3), (39, 2), (40, 4), (42, 0), (43, 5)]),
    ("Nike", "Pegasus 41", "Blue", "Men", "Running", 130, "blue", "|autumn|", [(41, 4), (42, 6), (43, 3), (44, 1)]),
    ("Nike", "V2K Run", "Silver", "Women", "Walking", 105, "silver", "|school|autumn|", [(35, 3), (36, 4), (37, 4), (38, 3), (39, 5), (40, 0)]),
    ("Adidas", "Samba OG", "White", "Unisex", "Lifestyle", 100, "white", "|school|autumn|", [(35, 2), (36, 3), (37, 2), (38, 2), (39, 5), (41, 4), (42, 2)]),
    ("Adidas", "Ultraboost Light", "Black", "Men", "Running", 190, "black", "|autumn|", [(41, 2), (42, 3), (43, 4), (44, 1)]),
    ("Adidas", "Gazelle Indoor", "Green", "Women", "Lifestyle", 120, "green", "|school|", [(35, 2), (36, 4), (37, 4), (38, 2), (39, 3), (40, 2)]),
    ("Adidas", "Superstar", "White", "Unisex", "Lifestyle", 90, "white", "|school|autumn|", [(36, 3), (37, 2), (38, 4), (39, 5), (40, 3), (42, 2), (43, 4)]),
    ("Adidas", "Campus 00s", "Grey", "Unisex", "Lifestyle", 110, "grey", "|school|autumn|", [(36, 2), (37, 2), (38, 3), (39, 5), (40, 2), (42, 4), (43, 1)]),
    ("New Balance", "550", "White", "Unisex", "Lifestyle", 115, "white", "|school|autumn|", [(36, 2), (37, 2), (38, 4), (39, 3), (40, 4), (42, 2), (43, 3)]),
    ("New Balance", "9060", "Beige", "Women", "Lifestyle", 150, "beige", "|autumn|", [(35, 1), (36, 3), (37, 2), (38, 4), (39, 3), (40, 1)]),
    ("New Balance", "Fresh Foam X 1080", "Blue", "Men", "Running", 165, "blue", "|autumn|", [(41, 3), (42, 5), (43, 2), (44, 4)]),
    ("New Balance", "2002R", "Grey", "Unisex", "Walking", 145, "grey", "|school|autumn|", [(39, 2), (40, 3), (42, 5), (43, 2)]),
    ("Puma", "Palermo", "Red", "Unisex", "Lifestyle", 90, "red", "|school|", [(35, 2), (36, 3), (37, 3), (38, 4), (39, 3), (41, 3), (42, 1)]),
    ("Puma", "RS-X Efekt", "Black", "Men", "Training", 125, "black", "|autumn|", [(41, 2), (42, 4), (43, 3), (44, 2)]),
    ("Asics", "GEL-Kayano 31", "White", "Women", "Running", 160, "white", "|autumn|", [(35, 2), (36, 3), (37, 2), (38, 5), (39, 2), (40, 3)]),
    ("Asics", "GEL-NYC", "Cream", "Unisex", "Lifestyle", 150, "cream", "|school|autumn|", [(36, 2), (37, 2), (38, 3), (39, 3), (40, 2), (42, 4), (43, 3)]),
    ("Nike", "Pegasus Trail 5", "Brown", "Unisex", "Trail", 145, "brown", "|water_resistant|autumn|", [(38, 2), (39, 2), (40, 2), (41, 4), (42, 3), (43, 2)]),
    ("Adidas", "Terrex Agravic 3", "Orange", "Unisex", "Trail", 155, "orange", "|water_resistant|autumn|", [(38, 2), (39, 3), (40, 3), (41, 2), (42, 4), (43, 1)]),
    ("Asics", "GEL-Trabuco 13", "Olive", "Men", "Trail", 150, "olive", "|water_resistant|autumn|", [(41, 2), (42, 3), (43, 3), (44, 1)]),
    ("Nike", "Air Force 1 '07", "White", "Unisex", "Lifestyle", 115, "white", "|school|autumn|", [(35, 2), (36, 4), (37, 3), (38, 3), (39, 4), (40, 3), (42, 4), (43, 2)]),
    ("Nike", "Winflo 11", "Black", "Women", "Running", 100, "black", "|autumn|", [(35, 2), (36, 3), (37, 3), (38, 4), (39, 2), (40, 3)]),
    ("Adidas", "Supernova Rise", "Blue", "Women", "Running", 140, "blue", "|autumn|", [(35, 1), (36, 2), (37, 2), (38, 4), (39, 3), (40, 2)]),
    ("Adidas", "Dropset 3", "Grey", "Men", "Training", 130, "grey", "|autumn|", [(41, 3), (42, 4), (43, 2), (44, 2)]),
    ("New Balance", "574", "Grey", "Unisex", "Lifestyle", 95, "grey", "|school|autumn|", [(35, 2), (36, 3), (37, 3), (38, 4), (39, 4), (40, 5), (42, 3), (43, 3)]),
    ("New Balance", "Fresh Foam Hierro v8", "Olive", "Men", "Trail", 155, "olive", "|water_resistant|autumn|", [(41, 2), (42, 4), (43, 3), (44, 1)]),
    ("Puma", "Tazon 7", "Black", "Men", "Training", 85, "black", "|school|", [(41, 3), (42, 5), (43, 4), (44, 2)]),
    ("Asics", "GEL-Sonoma 8 GTX", "Black", "Unisex", "Trail", 140, "black", "|water_resistant|autumn|", [(38, 2), (39, 3), (40, 3), (41, 3), (42, 4), (43, 2)]),
]


def sync_variants(product: Product, variants: list[tuple[int, int]], attributes_by_code: dict) -> None:
    """Приводит варианты товара и их значения оси в соответствие с данными каталога."""
    axis = SNEAKER_CONFIG.primary_axis
    existing = {variant_axis_value(variant, axis): variant for variant in product.variants}
    keep = set()
    for size, stock in variants:
        key = str(size)
        keep.add(key)
        variant = existing.get(key)
        if variant is None:
            variant = Variant(
                sku=f"{product.brand} {product.name} {key}".lower().replace(" ", "-"),
                price=product.base_price,
                stock=stock,
            )
            set_attribute_values(variant, {axis: key}, attributes_by_code)
            product.variants.append(variant)
        else:
            variant.stock = stock
            variant.price = product.base_price
    product.variants = [variant for variant in product.variants if variant_axis_value(variant, axis) in keep]


def seed_database(db: Session) -> None:
    """Добавляет отсутствующие демо-товары, сохраняя изменения владельца в существующих."""
    attributes_by_code = sync_attributes(db, SNEAKER_CONFIG)
    existing = {(product.brand, product.name): product for product in db.scalars(select(Product)).all()}
    for brand, name, color, gender, purpose, price, image, tags, variants in CATALOG:
        product = existing.get((brand, name))
        if product is not None:
            # Existing rows may have been edited in the local admin panel. Startup must not
            # silently revert their text, price, stock, photos, or publication status.
            continue
        product = Product(brand=brand, name=name, category=SNEAKER_CONFIG.category, base_price=price, image=image)
        db.add(product)
        db.flush()
        product.name = name
        product.category = SNEAKER_CONFIG.category
        product.base_price = price
        product.image = image
        product.active = True
        product.description = f"{brand} {name} в цвете {color}. Демонстрационная модель каталога."
        set_attribute_values(
            product,
            {
                "color": color,
                "gender": gender,
                "purpose": purpose,
                "tags": [tag for tag in tags.strip("|").split("|") if tag],
            },
            attributes_by_code,
        )
        sync_variants(product, variants, attributes_by_code)
    db.commit()
