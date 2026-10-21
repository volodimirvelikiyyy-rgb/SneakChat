import unittest

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.admin import ProductInput, _read_product, _save_product
from app.catalog import sync_attributes
from app.catalog_rules import SNEAKER_CONFIG
from app.database import Base
from app.models import Product


class CurrencyAdminTests(unittest.TestCase):
    def test_admin_saves_supported_currency_without_rewriting_price(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            sync_attributes(db, SNEAKER_CONFIG)
            product = Product(brand="Nike", name="Currency test", category="Sneakers", base_price=119, image="fallback")
            db.add(product)
            db.flush()
            for currency in ("USD", "EUR", "UAH"):
                payload = ProductInput(
                    brand=product.brand, name=product.name, color="White", price=119.25,
                    currency=currency, variants=[], status="draft",
                )
                _save_product(db, product, payload)
                db.commit()
                readback = _read_product(product)
                self.assertEqual(readback["price"], 119.25)
                self.assertEqual(readback["currency"], currency)
            with self.assertRaises(ValidationError):
                ProductInput(brand="Nike", name="Invalid", color="White", price=1, currency="GBP")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
