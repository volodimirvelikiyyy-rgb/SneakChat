"""Additive SQLite currency migration with an online backup and data invariants."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def _snapshot(connection: sqlite3.Connection) -> tuple[list[tuple], list[tuple], list[tuple]]:
    products = connection.execute(
        "SELECT id, base_price FROM products ORDER BY id"
    ).fetchall()
    variants = connection.execute(
        "SELECT id, product_id, price, stock FROM variants ORDER BY id"
    ).fetchall()
    sizes = connection.execute(
        "SELECT va.variant_id, va.value FROM variant_attributes va "
        "JOIN attributes a ON a.id = va.attribute_id WHERE a.code = 'size' "
        "ORDER BY va.variant_id"
    ).fetchall()
    return products, variants, sizes


def migrate_database(database_path: Path, backup_path: Path) -> tuple[int, int]:
    database_path = database_path.resolve()
    backup_path = backup_path.resolve()
    if database_path == backup_path or not database_path.is_file():
        raise RuntimeError("Database path is invalid")
    if backup_path.exists():
        raise FileExistsError(f"Refusing to overwrite backup: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(database_path, timeout=20)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"products", "variants", "attributes", "variant_attributes"}.issubset(tables):
            raise RuntimeError("Database does not have the expected SneakChat catalog schema")
        before = _snapshot(connection)
        backup = sqlite3.connect(backup_path)
        try:
            connection.backup(backup)
        finally:
            backup.close()

        product_columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
        request_columns = {row[1] for row in connection.execute("PRAGMA table_info(contact_requests)")} if "contact_requests" in tables else set()
        with connection:
            if "currency" not in product_columns:
                connection.execute(
                    "ALTER TABLE products ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD' "
                    "CHECK (currency IN ('USD', 'EUR', 'UAH'))"
                )
            if "contact_requests" in tables and "currency" not in request_columns:
                connection.execute(
                    "ALTER TABLE contact_requests ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD' "
                    "CHECK (currency IN ('USD', 'EUR', 'UAH'))"
                )
        after = _snapshot(connection)
        if before != after:
            raise RuntimeError("Migration changed product prices, variants, or sizes")
        currency_rows = connection.execute(
            "SELECT COUNT(*) FROM products WHERE currency != 'USD'"
        ).fetchone()[0]
        if currency_rows:
            raise RuntimeError("Existing products must retain USD currency")
        return len(before[0]), len(before[1])


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    db_path = root / "sneaker_store.db"
    backup = root / "backups" / "sneaker_store.pre-currency_2026-10-11.sqlite3"
    products, variants = migrate_database(db_path, backup)
    print(f"Currency migration complete: {products} products, {variants} variants; USD preserved.")
    print(f"Backup: {backup}")
