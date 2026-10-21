"""Additive, idempotent schema changes for product administration."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def ensure_admin_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "products" not in tables:
        raise RuntimeError("The existing SneakChat product catalog is missing")

    columns = {column["name"] for column in inspector.get_columns("products")}
    added_status = "admin_status" not in columns
    with engine.begin() as connection:
        if added_status:
            connection.execute(text("ALTER TABLE products ADD COLUMN admin_status VARCHAR(16) NOT NULL DEFAULT 'published'"))
            connection.execute(text("UPDATE products SET admin_status = CASE WHEN active = 1 THEN 'published' ELSE 'hidden' END"))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS product_images (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id),
                filename VARCHAR(80) NOT NULL UNIQUE,
                position INTEGER NOT NULL DEFAULT 0,
                is_primary BOOLEAN NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                crop_scale FLOAT NOT NULL DEFAULT 1.0,
                crop_x FLOAT NOT NULL DEFAULT 0.0,
                crop_y FLOAT NOT NULL DEFAULT 0.0
            )
        """))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_product_images_product_id ON product_images(product_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_product_images_is_active ON product_images(is_active)"))
