"""Проверки универсальности каталога.

Главный критерий: товар новой категории находится по своему атрибуту без единой строки
кода под этот атрибут. Здесь нет ни одного упоминания «storage» в приложении — оно есть
только в данных и в тесте.
"""

import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import catalog
from app.catalog import AttributeSpec, CatalogConfig, ProductQuery, set_attribute_values, sync_attributes
from app.catalog_rules import SNEAKER_CONFIG
from app.database import Base, SessionLocal, ensure_catalog_schema
from app.main import app
from app.models import Attribute, Product, ProductAttribute, Variant
from app.seed import seed_database
from app.services.assistant import parse_request
from app.services.products import serialize_product

# Конфигурация другого каталога: те же механизмы, другая категория товаров.
PHONE_CONFIG = CatalogConfig(
    code="phones",
    name="Смартфоны",
    category="Phone",
    attributes=(
        AttributeSpec(code="storage", name="Память", type="enum", scope="product"),
        AttributeSpec(code="color", name="Цвет", type="enum", scope="product"),
    ),
)


class UniversalCatalogTests(unittest.TestCase):
    """Ядро каталога проверяется на каталоге, которого в приложении раньше не было."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.attributes = sync_attributes(self.db, PHONE_CONFIG)
        self.phones = {}
        self._add_phone("Pixel 9", "Google", 700, storage="256GB", color="Black", variants=[("px9-256", 700, 3)])
        self._add_phone("Pixel 9", "Google", 820, storage="512GB", color="Black", variants=[("px9-512", 820, 2)])
        self._add_phone("iPhone 16", "Apple", 900, storage="256GB", color="White", variants=[("ip16-256", 900, 1)])
        self._add_phone("iPhone 16", "Apple", 1100, storage="512GB", color="White", variants=[("ip16-512", 1100, 0)])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _add_phone(self, name, brand, price, *, storage=None, color=None, variants=(), stock=None):
        product = Product(brand=brand, name=name, category="Phone", base_price=price, image="neutral")
        values = {}
        if storage is not None:
            values["storage"] = storage
        if color is not None:
            values["color"] = color
        set_attribute_values(product, values, self.attributes)
        product.variants = []
        for sku, variant_price, variant_stock in variants:
            variant = Variant(sku=sku, price=variant_price, stock=variant_stock)
            product.variants.append(variant)
        self.db.add(product)
        self.db.flush()
        self.phones.setdefault((name, storage), product)
        return product

    def _search(self, **attributes):
        return catalog.search_products(self.db, ProductQuery(attributes=attributes), PHONE_CONFIG)

    def test_phone_is_found_by_a_dynamic_attribute(self):
        """Основной критерий этапа: storage=256GB находится без кода под storage."""
        found = self._search(storage="256GB")
        self.assertEqual({product.name for product in found}, {"Pixel 9", "iPhone 16"})
        self.assertTrue(all(catalog.serialize_product(product)["attributes"]["storage"] == "256GB" for product in found))

    def test_brand_with_a_dynamic_attribute(self):
        found = self._search(storage="512GB")
        by_brand = catalog.search_products(
            self.db, ProductQuery(brand="Apple", attributes={"storage": "512GB"}), PHONE_CONFIG
        )
        self.assertEqual(len(found), 2)
        self.assertEqual([product.name for product in by_brand], ["iPhone 16"])

    def test_several_dynamic_attributes_at_once(self):
        found = catalog.search_products(
            self.db, ProductQuery(attributes={"storage": "256GB", "color": "White"}), PHONE_CONFIG
        )
        self.assertEqual([product.name for product in found], ["iPhone 16"])

    def test_product_with_several_variants(self):
        product = self.phones[("iPhone 16", "512GB")]
        product.variants.append(Variant(sku="ip16-512-b", price=1090, stock=4))
        self.db.commit()
        card = catalog.serialize_product(catalog.get_product(self.db, product.id))
        self.assertEqual(len(card["variants"]), 2)
        self.assertEqual(card["total_stock"], 4)
        self.assertIsNone(card["variants"][0]["attributes"].get("size"))

    def test_currency_is_serialized_and_price_ranges_stay_in_that_currency(self):
        euro_phone = self.phones[("Pixel 9", "256GB")]
        euro_phone.currency = "EUR"
        self.db.commit()
        euro_results = catalog.search_products(
            self.db, ProductQuery(min_price=600, max_price=750, currency="EUR"), PHONE_CONFIG
        )
        usd_results = catalog.search_products(
            self.db, ProductQuery(min_price=600, max_price=750, currency="USD"), PHONE_CONFIG
        )
        self.assertEqual([item.name for item in euro_results], ["Pixel 9"])
        self.assertEqual([item.currency for item in euro_results], ["EUR"])
        self.assertFalse(any(item.id == euro_phone.id for item in usd_results))

    def test_missing_attribute_value_gives_an_honest_empty_result(self):
        self.assertEqual(self._search(storage="128GB"), [])

    def test_insole_length_28cm_is_not_converted_without_catalog_data(self):
        """Длина стельки не становится EU-размером без такой оси и подтверждённых значений."""
        parsed = parse_request("длина стельки 28 см")
        attribute_codes = set(self.db.scalars(select(Attribute.code)).all())

        self.assertNotIn("size", parsed)
        self.assertNotIn("insole_length", attribute_codes)
        self.assertEqual(self._search(insole_length="28cm"), [])

    def test_unknown_attribute_gives_an_empty_result_without_special_code(self):
        self.assertEqual(self._search(weight="200g"), [])
        self.assertEqual(catalog.search_products(self.db, ProductQuery(attributes={"storage": "256GB", "weight": "1kg"}), PHONE_CONFIG), [])

    def test_product_without_variants_is_serialized_without_errors(self):
        product = self._add_phone("Pixel 9a", "Google", 500, storage="128GB", color="Green")
        self.db.commit()
        card = catalog.serialize_product(product)
        self.assertEqual(card["variants"], [])
        self.assertEqual(card["total_stock"], 0)
        self.assertEqual(card["attributes"]["storage"], "128GB")

    def test_inactive_products_are_not_returned(self):
        product = self.phones[("Pixel 9", "256GB")]
        product.active = False
        self.db.commit()
        self.assertEqual([item.name for item in self._search(storage="256GB")], ["iPhone 16"])


class SneakerConfigOnUniversalEngineTests(unittest.TestCase):
    """Каталог кроссовок работает через то же ядро: отличаются только конфигурация и данные."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_config_maps_legacy_keys_to_attribute_codes(self):
        query = catalog.build_query(SNEAKER_CONFIG, {"use_case": "Running", "feature": "school", "size": 43})
        self.assertEqual(
            query.attributes,
            {"purpose": "Running", "tags": "school", "size": "43"},
        )

    def test_unknown_key_is_treated_as_an_attribute_code(self):
        query = catalog.build_query(SNEAKER_CONFIG, {"storage": "256GB"})
        self.assertEqual(query.attributes, {"storage": "256GB"})

    def test_sizes_of_one_product_are_variant_attributes(self):
        product = self.db.scalar(select(Product).where(Product.name == "Pegasus 41"))
        card = serialize_product(product)
        self.assertEqual(card["available_sizes"], [41.0, 42.0, 43.0, 44.0])
        self.assertTrue(all(variant["size"] is not None for variant in card["variants"]))

    def test_one_product_with_several_sizes_is_found_by_any_of_them(self):
        for size in (41, 42, 43, 44):
            with self.subTest(size=size):
                products = catalog.search_products(
                    self.db, catalog.build_query(SNEAKER_CONFIG, {"brand": "Nike", "size": size}), SNEAKER_CONFIG
                )
                self.assertIn("Pegasus 41", [product.name for product in products])

    def test_unavailable_size_is_not_found(self):
        # Размер 46 в демо-каталоге не продаётся: вариант не должен «находиться» без остатка.
        products = catalog.search_products(
            self.db, catalog.build_query(SNEAKER_CONFIG, {"size": 46}), SNEAKER_CONFIG
        )
        self.assertEqual(products, [])

    def test_several_attributes_at_once(self):
        products = catalog.search_products(
            self.db,
            catalog.build_query(SNEAKER_CONFIG, {"color": "Black", "use_case": "Trail", "size": 43}),
            SNEAKER_CONFIG,
        )
        self.assertTrue(products)
        for product in products:
            card = serialize_product(product)
            self.assertEqual(card["color"], "Black")
            self.assertEqual(card["use_case"], "Trail")
            self.assertIn(43.0, card["available_sizes"])

    def test_name_query_is_a_literal_match_not_a_pattern(self):
        self.assertEqual(catalog.search_products(self.db, ProductQuery(name_query="%"), SNEAKER_CONFIG), [])
        self.assertEqual(catalog.search_products(self.db, ProductQuery(name_query="_"), SNEAKER_CONFIG), [])
        names = [
            product.name
            for product in catalog.search_products(self.db, ProductQuery(name_query="pegasus"), SNEAKER_CONFIG)
        ]
        self.assertEqual(names, ["Pegasus 41", "Pegasus Trail 5"])

    def test_numeric_attribute_filter_uses_numbers(self):
        products = catalog.search_products(
            self.db, catalog.build_query(SNEAKER_CONFIG, {"min_price": 100, "max_price": 110}), SNEAKER_CONFIG
        )
        self.assertTrue(products)
        self.assertTrue(all(100 <= serialize_product(product)["price"] <= 110 for product in products))


class CoreIsCatalogAgnosticTests(unittest.TestCase):
    """Ядро не должно знать осей конкретного каталога."""

    def test_core_module_has_no_catalog_specific_rules(self):
        source = Path(catalog.__file__).read_text(encoding="utf-8").lower()
        for word in ("sneaker", "кроссов", "размер", "цвет", "storage", "purpose", "gender"):
            with self.subTest(word=word):
                self.assertNotIn(word, source)

    def test_core_has_no_attribute_equality_branches(self):
        source = Path(catalog.__file__).read_text(encoding="utf-8")
        self.assertNotIn('== "size"', source)
        self.assertNotIn("attribute == ", source)


class UniversalApiTests(unittest.TestCase):
    """API принимает произвольные атрибуты и отдаёт карточку товара другой категории."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.client = TestClient(app)
        self.attributes = sync_attributes(self.db, PHONE_CONFIG)
        self.db.commit()

    def tearDown(self):
        # Телефон и его атрибуты добавляются в демо-каталог только на время проверки.
        phone = self.db.query(Product).filter(Product.category == "Phone").one_or_none()
        if phone is not None:
            self.db.delete(phone)
            self.db.flush()
        storage = self.db.query(Attribute).filter(Attribute.code == "storage").one_or_none()
        if storage is not None:
            used = self.db.query(ProductAttribute).filter(ProductAttribute.attribute_id == storage.id).first()
            if used is None:
                self.db.delete(storage)
        self.db.commit()

    def _create_phone(self) -> Product:
        product = Product(brand="Google", name="Pixel 9", category="Phone", base_price=700, image="neutral")
        set_attribute_values(product, {"storage": "256GB", "color": "Black"}, self.attributes)
        variant = Variant(sku="px9-256", price=700, stock=3)
        product.variants = [variant]
        self.db.add(product)
        self.db.commit()
        return product

    def test_search_by_generic_attribute_parameter(self):
        self._create_phone()
        response = self.client.get("/api/products/search", params={"attribute": "storage:256GB"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["name"] for item in payload], ["Pixel 9"])
        self.assertEqual(payload[0]["category"], "Phone")
        self.assertEqual(payload[0]["attributes"]["storage"], "256GB")
        self.assertIsNone(payload[0]["variants"][0]["size"])

    def test_generic_attribute_param_works_together_with_brand(self):
        self._create_phone()
        response = self.client.get(
            "/api/products/search", params=[("brand", "Google"), ("attribute", "storage:256GB")]
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_unknown_attribute_returns_empty_list(self):
        self._create_phone()
        response = self.client.get("/api/products/search", params={"attribute": "storage:64GB"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_sneaker_search_still_works(self):
        response = self.client.get("/api/products/search", params={"color": "Black", "size": 43, "use_case": "Trail"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload)
        self.assertTrue(all(item["use_case"] == "Trail" for item in payload))
        self.assertTrue(all(43.0 in item["available_sizes"] for item in payload))


if __name__ == "__main__":
    unittest.main()
