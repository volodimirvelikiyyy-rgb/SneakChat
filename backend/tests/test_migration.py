"""Перенос данных со схемы прежних этапов (оси колонками) на универсальный каталог."""

import os
import tempfile
import unittest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import database
from app.database import ensure_catalog_schema
from app.migration import relink_contact_requests
from app.seed import CATALOG, seed_database
from app.services.requests import list_contact_requests

LEGACY_SCHEMA = (
    "CREATE TABLE products (id INTEGER PRIMARY KEY, brand VARCHAR(50), name VARCHAR(120), color VARCHAR(60),"
    " gender VARCHAR(20), use_case VARCHAR(30), feature_tags VARCHAR(180), price NUMERIC(10, 2), image_key VARCHAR(40))",
    "CREATE TABLE product_variants (id INTEGER PRIMARY KEY, product_id INTEGER, size NUMERIC(4, 1), stock INTEGER)",
    "CREATE TABLE contact_requests (id INTEGER PRIMARY KEY, product_id INTEGER, variant_id INTEGER,"
    " name VARCHAR(100), contact VARCHAR(150), created_at DATETIME)",
)


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = handle.name
        self.url = f"sqlite:///{self.path}"
        legacy = create_engine(self.url)
        with legacy.begin() as connection:
            for statement in LEGACY_SCHEMA:
                connection.execute(text(statement))
            connection.execute(
                text(
                    "INSERT INTO products (id, brand, name, color, gender, use_case, feature_tags, price, image_key)"
                    " VALUES (7, 'Nike', 'Pegasus 41', 'Blue', 'Men', 'Running', '|autumn|', 130, 'blue')"
                )
            )
            connection.execute(text("INSERT INTO product_variants (id, product_id, size, stock) VALUES (9, 7, 43, 3)"))
            connection.execute(
                text(
                    "INSERT INTO contact_requests (id, product_id, variant_id, name, contact, created_at)"
                    " VALUES (1, 7, 9, 'Client', '@client', '2026-01-01 10:00:00')"
                )
            )
        legacy.dispose()
        self.original_engine = database.engine
        database.engine = create_engine(self.url, connect_args={"check_same_thread": False})

    def tearDown(self):
        database.engine.dispose()
        database.engine = self.original_engine
        os.unlink(self.path)

    def test_legacy_schema_is_rebuilt_without_dropping_requests(self):
        links = ensure_catalog_schema()
        self.assertEqual(len(links), 1, "ссылка заявки должна быть снята до пересоздания каталога")

        tables = set(inspect(database.engine).get_table_names())
        self.assertNotIn("product_variants", tables)
        self.assertIn("attributes", tables)
        with database.engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("products")}
        self.assertNotIn("color", columns)
        self.assertIn("base_price", columns)

        with Session(database.engine) as db:
            seed_database(db)
            self.assertEqual(relink_contact_requests(db, links), {"restored": 1, "cleared": 0})
            rows = list_contact_requests(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product"], "Nike Pegasus 41")
        self.assertEqual(rows[0]["size"], 43, "заявка должна вернуться к своему размеру")
        self.assertEqual(rows[0]["name"], "Client")
        self.assertEqual(rows[0]["contact"], "@client", "контакт заявки должен сохраниться")
        self.assertEqual(rows[0]["currency"], "USD", "заявка старой схемы должна получить базовую валюту каталога")

    def test_request_of_a_removed_product_is_cleared_and_still_visible(self):
        # Идентификаторы после пересоздания переиспользуются, поэтому прежнюю ссылку оставлять
        # нельзя: заявка начала бы показывать чужую модель. Ссылку обнуляем, строку не скрываем.
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO products (id, brand, name, color, gender, use_case, feature_tags, price, image_key)"
                    " VALUES (11, 'Nike', 'Discontinued Model', 'White', 'Men', 'Lifestyle', '|school|', 100, 'white')"
                )
            )
            connection.execute(text("INSERT INTO product_variants (id, product_id, size, stock) VALUES (12, 11, 42, 1)"))
            connection.execute(
                text(
                    "INSERT INTO contact_requests (id, product_id, variant_id, name, contact, created_at)"
                    " VALUES (2, 11, 12, 'Ghost', '@ghost', '2026-01-02 10:00:00')"
                )
            )
        links = ensure_catalog_schema()
        with Session(database.engine) as db:
            seed_database(db)
            result = relink_contact_requests(db, links)
            rows = {row["name"]: row for row in list_contact_requests(db)}
        self.assertEqual(result, {"restored": 1, "cleared": 1})
        self.assertEqual(rows["Ghost"]["product"], "")
        self.assertIsNone(rows["Ghost"]["size"])
        self.assertEqual(rows["Client"]["product"], "Nike Pegasus 41")

    def test_non_numeric_axis_value_does_not_break_relinking(self):
        # Ось варианта не обязана быть числовой: значение «XL» не должно ронять перенос.
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE product_variants SET size = 'XL' WHERE id = 9"))
        links = ensure_catalog_schema()
        self.assertEqual(links[0]["size"], "XL")
        with Session(database.engine) as db:
            seed_database(db)
            result = relink_contact_requests(db, links)
            rows = list_contact_requests(db)
        self.assertEqual(result["restored"], 1)
        self.assertEqual(rows[0]["product"], "Nike Pegasus 41")
        self.assertIsNone(rows[0]["size"])

    def test_rebuild_is_idempotent(self):
        ensure_catalog_schema()
        self.assertEqual(ensure_catalog_schema(), [])
        with Session(database.engine) as db:
            seed_database(db)
            self.assertEqual(db.execute(text("SELECT COUNT(*) FROM products")).scalar(), len(CATALOG))


if __name__ == "__main__":
    unittest.main()
