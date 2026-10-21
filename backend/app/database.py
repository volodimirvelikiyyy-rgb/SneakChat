import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def validate_test_database_url(database_url: str | None) -> str:
    """Require an explicit file-backed SQLite database below the system temp directory."""
    if not database_url:
        raise RuntimeError("Set SNEAKCHAT_DATABASE_URL to a temporary SQLite file before running tests or fixtures")
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        raise RuntimeError("Tests and fixtures require an explicit file-backed temporary SQLite database")
    path = Path(url.database)
    if not path.is_absolute():
        raise RuntimeError("Tests and fixtures refuse relative SQLite paths")
    resolved = path.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved.relative_to(temp_root)
    except ValueError as exc:
        raise RuntimeError("Tests and fixtures require SNEAKCHAT_DATABASE_URL inside the system temp directory") from exc
    working_database = Path(__file__).resolve().parents[1] / "sneaker_store.db"
    if resolved == working_database.resolve():
        raise RuntimeError("Tests and fixtures refuse the working SneakChat database")
    return database_url


_test_process = (
    "pytest" in sys.modules
    or "unittest" in sys.modules
    or Path(sys.argv[0]).name.casefold() == "run_test_import.py"
)
_configured_database_url = os.environ.get("SNEAKCHAT_DATABASE_URL")
DATABASE_URL = (
    validate_test_database_url(_configured_database_url)
    if _test_process
    else (_configured_database_url or "sqlite:///./sneaker_store.db")
)

# Колонки, по которым видно схему каталога прежних этапов: оси были полями товара.
LEGACY_PRODUCT_COLUMNS = {"color", "gender", "use_case", "feature_tags", "image_key", "price"}
# Таблицы каталога: их можно пересоздать, потому что каталог целиком воспроизводится сидом.
CATALOG_TABLES = (
    "variant_attributes",
    "product_attributes",
    "variants",
    "product_variants",  # имя таблицы вариантов на прежних этапах
    "products",
    "attributes",
)
# Колонки заявок, которые могли отсутствовать в базе прежних этапов.
REQUEST_COLUMNS = {
    "product_id": "ALTER TABLE contact_requests ADD COLUMN product_id INTEGER NOT NULL DEFAULT 0",
    "variant_id": "ALTER TABLE contact_requests ADD COLUMN variant_id INTEGER NOT NULL DEFAULT 0",
    "price": "ALTER TABLE contact_requests ADD COLUMN price NUMERIC(10, 2) NOT NULL DEFAULT 0",
    # Legacy catalog/request prices were stored in the base USD currency. Keep the same
    # default as the standalone currency migration and current ContactRequest model.
    "currency": "ALTER TABLE contact_requests ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency IN ('USD', 'EUR', 'UAH'))",
    "comment": "ALTER TABLE contact_requests ADD COLUMN comment TEXT",
    "status": "ALTER TABLE contact_requests ADD COLUMN status VARCHAR(30) NOT NULL DEFAULT 'new'",
    "created_at": "ALTER TABLE contact_requests ADD COLUMN created_at DATETIME",
}


class Base(DeclarativeBase):
    pass


engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _capture_request_links(tables: set[str]) -> list[dict]:
    """Снимает ссылки заявок на товар и вариант до пересоздания каталога.

    Ссылки нужны, чтобы после пересидирования каталога заявки не начали указывать на чужие
    товары: идентификаторы при пересоздании меняются.
    """
    if "contact_requests" not in tables:
        return []
    columns = "requests.id AS request_id, products.brand AS brand, products.name AS name"
    joins = "JOIN products ON products.id = requests.product_id"
    if "product_variants" in tables:
        columns += ", variants.size AS size"
        joins += " LEFT JOIN product_variants AS variants ON variants.id = requests.variant_id"
    with engine.begin() as connection:
        rows = connection.execute(
            text(f"SELECT {columns} FROM contact_requests AS requests {joins}")
        ).mappings().all()
    return [dict(row) for row in rows]


def ensure_catalog_schema() -> list[dict]:
    """Готовит схему к универсальному каталогу.

    Если таблицы каталога остались от схемы с осями-колонками, они пересоздаются: каталог
    полностью воспроизводится сидом, поэтому потери данных каталога нет. Таблица заявок
    сохраняется, а ссылки заявок возвращаются функцией `migration.relink_contact_requests`
    после пересидирования.

    Возвращает снятые ссылки заявок (пустой список, если пересоздания не было).
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "contact_requests" in tables:
        columns = {column["name"] for column in inspector.get_columns("contact_requests")}
        with engine.begin() as connection:
            for name, statement in REQUEST_COLUMNS.items():
                if name not in columns:
                    connection.execute(text(statement))

    links: list[dict] = []
    if "products" in tables:
        columns = {column["name"] for column in inspector.get_columns("products")}
        if columns & LEGACY_PRODUCT_COLUMNS:
            links = _capture_request_links(tables)
            with engine.begin() as connection:
                for name in CATALOG_TABLES:
                    table = Base.metadata.tables.get(name)
                    if table is not None:
                        table.drop(bind=connection, checkfirst=True)
                    else:
                        # Таблицы прежней схемы нет в метаданных, имя берётся из константы.
                        connection.execute(text(f"DROP TABLE IF EXISTS {name}"))

    Base.metadata.create_all(bind=engine)
    # Admin schema is additive and idempotent; it does not reseed or replace catalog rows.
    from .admin_migration import ensure_admin_schema

    ensure_admin_schema(engine)
    return links


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
