import os
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import Base, engine, validate_test_database_url
from app.models import Product, Variant
from app.importer import import_from_csv

# Fixture imports must run against an explicitly configured temporary SQLite file.
validate_test_database_url(os.environ.get("SNEAKCHAT_DATABASE_URL"))
Base.metadata.create_all(engine)
session = Session(engine)

# 3. Запускаем импорт
print("Запуск импорта...")
result = import_from_csv(session, "test_import.csv")
print(f"Результат: {result}")

# 4. Проверяем результаты
if result.get("ok"):
    # Проверяем новый товар
    gazelle = session.scalar(select(Product).where(Product.name == "Gazelle Custom"))
    if gazelle:
        print(f"SUCCESS: Найден новый товар: {gazelle.brand} {gazelle.name}, цена: {gazelle.base_price}")
        print(f"Варианты (остатки): {[v.stock for v in gazelle.variants]}")
    else:
        print("FAIL: Новый товар не найден")

    # Проверяем обновление цены
    pulse = session.scalar(select(Product).where(Product.name == "Air Max Pulse"))
    if pulse and pulse.base_price == 150:
        print(f"SUCCESS: Цена Nike Air Max Pulse обновлена до 150")
    else:
        print(f"FAIL: Цена не обновилась (текущая: {pulse.base_price if pulse else 'N/A'})")

session.close()
