import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.currency_migration import migrate_database


class CurrencyMigrationTests(unittest.TestCase):
    def test_adds_usd_defaults_and_preserves_prices_variants_and_sizes(self):
        with tempfile.TemporaryDirectory(prefix="sneakchat-currency-migration-") as folder:
            root = Path(folder)
            database = root / "sneaker_store.db"
            backup = root / "before.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript("""
                    CREATE TABLE products (id INTEGER PRIMARY KEY, base_price NUMERIC NOT NULL);
                    CREATE TABLE variants (id INTEGER PRIMARY KEY, product_id INTEGER, price NUMERIC, stock INTEGER);
                    CREATE TABLE attributes (id INTEGER PRIMARY KEY, code TEXT);
                    CREATE TABLE variant_attributes (id INTEGER PRIMARY KEY, variant_id INTEGER, attribute_id INTEGER, value TEXT);
                    CREATE TABLE contact_requests (id INTEGER PRIMARY KEY, price NUMERIC NOT NULL);
                    INSERT INTO products VALUES (1, 84.50), (2, 121);
                    INSERT INTO variants VALUES (11, 1, 84.50, 3), (22, 2, 121, 5);
                    INSERT INTO attributes VALUES (7, 'size');
                    INSERT INTO variant_attributes VALUES (1, 11, 7, '42'), (2, 22, 7, '44');
                    INSERT INTO contact_requests VALUES (1, 84.50);
                """)

            self.assertEqual(migrate_database(database, backup), (2, 2))
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT id, base_price, currency FROM products ORDER BY id").fetchall(),
                                 [(1, 84.5, "USD"), (2, 121, "USD")])
                self.assertEqual(connection.execute("SELECT id, product_id, price, stock FROM variants ORDER BY id").fetchall(),
                                 [(11, 1, 84.5, 3), (22, 2, 121, 5)])
                self.assertEqual(connection.execute("SELECT variant_id, value FROM variant_attributes ORDER BY id").fetchall(),
                                 [(11, "42"), (22, "44")])
                self.assertEqual(connection.execute("SELECT currency FROM contact_requests").fetchall(), [("USD",)])
            with closing(sqlite3.connect(backup)) as connection:
                self.assertNotIn("currency", {row[1] for row in connection.execute("PRAGMA table_info(products)")})


if __name__ == "__main__":
    unittest.main()
