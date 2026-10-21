import unittest
from unittest.mock import patch

from app.api.currency import read_currency_rates
from app.database import Base
from app.models import Product
from app.services.products import find_products
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


class CurrencyApiTests(unittest.TestCase):
    def test_returns_rate_snapshot_and_date_from_currency_service(self):
        result = {"available": True, "rates": {"EUR": 1, "USD": 1.1, "UAH": 44},
                  "rate_date": "2026-10-10", "stale": False, "source": "Frankfurter"}
        with patch("app.api.currency.get_rates", return_value=result):
            self.assertEqual(read_currency_rates(), result)

    def test_unavailable_snapshot_is_explicit_instead_of_a_fabricated_rate(self):
        result = {"available": False, "rates": None, "rate_date": None,
                  "stale": False, "source": "Frankfurter"}
        with patch("app.api.currency.get_rates", return_value=result):
            self.assertFalse(read_currency_rates()["available"])
            self.assertIsNone(read_currency_rates()["rates"])

    def test_display_currency_budget_filters_mixed_source_currencies(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as db:
                db.add_all([
                    Product(brand="Test", name="USD Shoe", category="Footwear", base_price=100,
                            currency="USD", image="neutral", active=True),
                    Product(brand="Test", name="EUR Shoe", category="Footwear", base_price=100,
                            currency="EUR", image="neutral", active=True),
                    Product(brand="Test", name="UAH Shoe", category="Footwear", base_price=4400,
                            currency="UAH", image="neutral", active=True),
                ])
                db.commit()
                snapshot = {"available": True, "rates": {"EUR": 1.0, "USD": 1.1, "UAH": 44.0}}
                with patch("app.services.products.get_rates", return_value=snapshot):
                    found = find_products(db, max_price=101, currency="EUR", display_currency="EUR")
                self.assertEqual({item.name for item in found}, {"USD Shoe", "EUR Shoe", "UAH Shoe"})
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
