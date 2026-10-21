import csv
import io
import os
from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import set_attribute_values, sync_attributes, variant_axis_value
from .catalog_rules import SNEAKER_CONFIG
from .models import Product, Variant


def parse_variants(variants_str: str) -> list[tuple[str, int]]:
    """Парсит строку вида '39:2, 40:5' в список кортежей."""
    result = []
    if not variants_str or not str(variants_str).strip():
        return result
    for part in str(variants_str).split(","):
        if ":" in part:
            val, stock = part.split(":")
            try:
                result.append((val.strip(), int(stock.strip())))
            except ValueError:
                continue
    return result


def sync_variants_universal(product: Product, variants: list[tuple[str, int]], attributes_by_code: dict, config) -> None:
    """Синхронизирует варианты товара на основе универсальной оси из конфига."""
    axis = config.primary_axis
    if not axis:
        return
        
    existing = {variant_axis_value(variant, axis): variant for variant in product.variants}
    keep = set()
    
    for val, stock in variants:
        keep.add(val)
        variant = existing.get(val)
        if variant is None:
            variant = Variant(
                sku=f"{product.brand}-{product.name}-{val}".lower().replace(" ", "-"),
                price=product.base_price,
                stock=stock,
            )
            set_attribute_values(variant, {axis: val}, attributes_by_code)
            product.variants.append(variant)
        else:
            variant.stock = stock
            variant.price = product.base_price
            
    # Удаляем варианты, которых нет в новом списке
    product.variants = [v for v in product.variants if variant_axis_value(v, axis) in keep]


def import_from_csv(db: Session, csv_path: str, config=SNEAKER_CONFIG) -> dict:
    """Импортирует каталог из CSV файла."""
    if not os.path.exists(csv_path):
        return {"ok": False, "error": f"Файл не найден: {csv_path}"}

    try:
        attributes_by_code = sync_attributes(db, config)
        existing_products = {(p.brand.lower(), p.name.lower()): p for p in db.scalars(select(Product)).all()}
        
        imported_count = 0
        with open(csv_path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                brand = row.get("Brand", "Other").strip()
                name = row.get("Name", "Unnamed").strip()
                price = float(row.get("Price", 0))
                category = row.get("Category", config.category).strip()
                
                key = (brand.lower(), name.lower())
                product = existing_products.get(key)
                
                if product is None:
                    product = Product(brand=brand, name=name, category=category, base_price=price)
                    db.add(product)
                
                product.base_price = price
                product.description = row.get("Description", "").strip()
                product.image = row.get("Image", "neutral").strip()
                product.active = True
                
                # Собираем атрибуты из колонок, которые есть в конфиге
                attr_values = {}
                for spec in config.attributes:
                    if spec.code in row:
                        val = row[spec.code].strip()
                        if spec.multi and "|" in val:
                            attr_values[spec.code] = [v for v in val.split("|") if v]
                        else:
                            attr_values[spec.code] = val
                
                set_attribute_values(product, attr_values, attributes_by_code)
                
                # Обработка вариантов (размеры и т.д.)
                if "Variants" in row:
                    variants_data = parse_variants(row["Variants"])
                    sync_variants_universal(product, variants_data, attributes_by_code, config)
                
                imported_count += 1
        
        db.commit()
        return {"ok": True, "count": imported_count}
        
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
