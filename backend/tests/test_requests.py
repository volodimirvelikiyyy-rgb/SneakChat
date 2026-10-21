import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.catalog import set_attribute_values
from app.catalog_rules import SNEAKER_CONFIG
from app.database import Base
from app.models import Attribute, Product, Variant
from app.schemas import ContactRequestCreate
from app.services.requests import create_contact_request, delete_contact_request, list_contact_requests, update_contact_request_status


class ContactRequestTests(unittest.TestCase):
    def test_request_contains_product_size_price_comment_and_status(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            # Товар и его вариант описываются общими полями и атрибутами каталога.
            attributes = {
                spec.code: Attribute(code=spec.code, name=spec.name, type=spec.type)
                for spec in SNEAKER_CONFIG.attributes
            }
            db.add_all(attributes.values())
            product = Product(brand="Nike", name="Test", category="Sneakers", base_price=99, currency="EUR", image="white")
            set_attribute_values(product, {"color": "White", "gender": "Unisex"}, attributes)
            variant = Variant(sku="nike-test-43", price=99, stock=2)
            set_attribute_values(variant, {"size": "43"}, attributes)
            product.variants = [variant]
            db.add(product)
            db.commit()
            payload = ContactRequestCreate(product_id=product.id, variant_id=product.variants[0].id, name="Client", contact="@client", comment="Call me")
            saved = create_contact_request(db, payload)
            rows = list_contact_requests(db)
            self.assertIsNotNone(saved)
            self.assertEqual(rows[0]["product"], "Nike Test")
            self.assertEqual(rows[0]["size"], 43)
            self.assertEqual(rows[0]["price"], 99)
            self.assertEqual(rows[0]["currency"], "EUR")
            self.assertEqual(rows[0]["comment"], "Call me")
            self.assertEqual(rows[0]["status"], "new")

            updated = update_contact_request_status(db, saved.id, "contacted")
            self.assertIsNotNone(updated)
            self.assertEqual(list_contact_requests(db)[0]["status"], "contacted")

            self.assertTrue(delete_contact_request(db, saved.id))
            self.assertEqual(list_contact_requests(db), [])
            self.assertFalse(delete_contact_request(db, saved.id))


if __name__ == "__main__":
    unittest.main()
