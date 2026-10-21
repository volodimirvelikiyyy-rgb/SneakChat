"""Ambiguous shopping requests use isolated catalog data, never production rows."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.grounding import verify_products, verify_reply, verify_reasons
from app.catalog import set_attribute_values, sync_attributes
from app.catalog_rules import SNEAKER_CONFIG
from app.database import Base, validate_test_database_url
from app.models import Attribute, Product, Variant
from app.services.assistant import parse_request
from app.services.chat import _sessions, chat
from app.services.chat_sessions import load_session_state
from app.services.request_safety import verified_measurement_sizes


class RequestSafetyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"AI_PROVIDER": "disabled"})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory(prefix="sneakchat-request-safety-")
        self.engine = create_engine("sqlite:///" + (Path(self.temp.name) / "tests.sqlite").as_posix())
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.attrs = sync_attributes(self.db, SNEAKER_CONFIG)
        self.products = {}
        for generation, price in (("v13", 160), ("v14", 180), ("v130", 200)):
            product = Product(brand="New Balance", name="Fresh Foam X 1080 " + generation,
                              category="Sneakers", base_price=price, currency="USD")
            set_attribute_values(product, {"color": "Black", "purpose": "Running", "gender": "Unisex"}, self.attrs)
            for size, stock in ((42, 2), (43, 1), (44, 0)):
                variant = Variant(sku=generation + "-" + str(size), price=price, stock=stock)
                set_attribute_values(variant, {"size": size}, self.attrs)
                product.variants.append(variant)
            self.db.add(product)
            self.products[generation] = product
        self.db.commit()
        self.sid = "safety-" + Path(self.temp.name).name

    def tearDown(self):
        _sessions.pop(self.sid, None)
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()
        self.env.stop()

    def send(self, message, language="uk"):
        result = chat(self.db, self.sid, message, language)
        self.assertEqual(verify_products(self.db, result["products"]), [])
        self.assertEqual(verify_reply(result["reply"], result["products"], result["filters"]), [])
        self.assertEqual(verify_reasons(result["products"]), [])
        return result


    def fresh(self):
        self.db.rollback()
        _sessions.pop(self.sid, None)
        self.sid += "-new"

    def add_chart(self, generation="v13", verified="true"):
        product = self.products[generation]
        codes = ("size_chart_source", "size_chart_verified", "size_chart_brand", "size_chart_model",
                 "insole_cm", "foot_cm", "cm_jp")
        for code in codes:
            attr = Attribute(code=code, name=code, type="string")
            self.db.add(attr)
            self.attrs[code] = attr
        self.db.flush()
        set_attribute_values(product, {
            "color": "Black", "purpose": "Running", "gender": "Unisex",
            "size_chart_source": "https://manufacturer.example/verified-test-chart",
            "size_chart_verified": verified, "size_chart_brand": product.brand,
            "size_chart_model": product.name,
        }, self.attrs)
        for variant in product.variants:
            size = int(next(row.value for row in variant.attributes if row.attribute.code == "size"))
            set_attribute_values(variant, {"size": size, "insole_cm": 28 if size == 42 else 29,
                                          "foot_cm": 27, "cm_jp": 27.5}, self.attrs)
        self.db.commit()
        return product

    def test_bare_and_compact_centimetres_ask_one_specific_question(self):
        self.add_chart()
        for message in ("28 см", "кросівки 28см"):
            with self.subTest(message=message):
                self.fresh()
                response = self.send(message)
                self.assertEqual(response["reply"], "Це довжина стопи чи устілки?")
                self.assertEqual(response["products"], [])
                self.assertNotIn("size", response["filters"])
                self.assertEqual(response["reply"].count("?"), 1)

    def test_measurement_type_is_asked_only_when_a_chart_can_answer(self):
        response = self.send('New Balance 1080 v13 28,5 см')
        self.assertNotIn('стопи чи устілки', response['reply'])
        self.assertEqual(_sessions[self.sid]['dialogue']['missing'], ['eu_size'])
        self.assertIsNone(_sessions[self.sid]['clarification']['measurement']['kind'])

    def test_verified_chart_short_answers_and_cache_reload(self):
        self.add_chart()
        self.send('New Balance 1080 v13')
        self.send('CM 28')
        self.assertEqual(_sessions[self.sid]['dialogue']['missing'], ['measurement_kind'])
        self.send('так')
        self.assertIsNone(_sessions[self.sid]['clarification']['measurement']['kind'])
        _sessions.pop(self.sid, None)
        response = self.send('устілка')
        self.assertEqual(response['products'][0]['matching_sizes'], [42])
        self.assertTrue(_sessions[self.sid]['filters']['name_query'].endswith('1080 v13'))

    def test_known_eu_does_not_trigger_another_size_question_on_measurement(self):
        self.send('New Balance 1080 v13 розмір 42')
        response = self.send('28,5 см')
        self.assertNotIn('?', response['reply'])
        self.assertEqual(response['filters']['size'], 42)
        self.assertIn('не підтверджена', response['reply'])

    def test_requested_measurement_without_value_uses_only_verified_model_table(self):
        self.add_chart()
        self.send('New Balance 1080 v13')
        response = self.send('довжина устілки')
        self.assertEqual(_sessions[self.sid]['dialogue']['missing'], ['eu_size'])
        response = self.send('42')
        self.assertIn('устілка 28 см', response['reply'])
        self.assertIn('EU 42', response['reply'])

    def test_measurement_kind_after_value_is_retained(self):
        self.send('28,5 см по стельке', 'ru')
        self.assertEqual(_sessions[self.sid]['clarification']['measurement'], {'value': '28.5', 'kind': 'insole_cm'})

    def test_explicit_insole_and_foot_do_not_repeat_kind_question(self):
        for message, kind in (("устілка 28 см", "insole_cm"), ("стопа 28 см", "foot_cm"),
                              ("Нужны кроссовки на стельку 28 см", "insole_cm"),
                              ("CM/JP 28", "cm_jp"), ("CM 28", None)):
            with self.subTest(message=message):
                self.fresh()
                response = self.send(message)
                self.assertNotIn("довжина стопи чи устілки", response["reply"])
                self.assertEqual(_sessions[self.sid]["clarification"]["measurement"]["kind"], kind)
                self.assertNotIn("size", response["filters"])
                self.assertEqual(response["products"], [])
                self.assertIn("підтвердити", response["reply"])

    def test_centimetres_are_never_eu_sizes_or_out_of_range_sizes(self):
        for message in ("28 см", "розмір 28 см", "44cm", "CM 44"):
            with self.subTest(message=message):
                self.fresh()
                self.assertNotIn("size", parse_request(message))
                result = self.send(message)
                self.assertNotIn("size", result["filters"])
                self.assertEqual(result["products"], [])
                self.assertNotIn("межами", result["reply"])

    def test_kind_answer_and_questions_survive_cache_reset(self):
        self.add_chart()
        self.send("28 см")
        _sessions.pop(self.sid, None)
        response = self.send("устілка")
        self.assertNotIn("довжина стопи чи устілки", response["reply"])
        self.assertEqual(load_session_state(self.db, self.sid)["clarification"]["measurement"]["kind"], "insole_cm")
        _sessions.pop(self.sid, None)
        repeated = self.send("Покажи ще")
        self.assertNotIn("?", repeated["reply"])
        self.assertEqual(repeated["products"], [])

    def test_repeating_unknown_measurement_does_not_repeat_question(self):
        self.send("28 см")
        response = self.send("28 см")
        self.assertNotIn("?", response["reply"])
        response = self.send("стопа")
        self.assertIn("підтвердити", response["reply"])
        self.assertLessEqual(response["reply"].count("?"), 1)

    def test_explicit_eu_answer_preserves_known_measurement_and_context(self):
        self.send("New Balance до $190")
        self.send("устілка 28 см")
        response = self.send("розмір 43")
        self.assertTrue(response["products"])
        self.assertEqual(response["filters"]["size"], 43)
        self.assertEqual(response["filters"]["brand"], "New Balance")
        self.assertEqual(response["filters"]["max_price"], 190)
        self.assertIn("не підтверджена", response["reply"])
        self.assertTrue(all(43 in item["available_sizes"] for item in response["products"]))

    def test_family_without_generation_shows_catalog_generations(self):
        response = self.send("Fresh Foam X 1080")
        self.assertFalse(response["needs_clarification"])
        self.assertEqual({item["name"] for item in response["products"]},
                         {product.name for product in self.products.values()})
        self.assertEqual(_sessions[self.sid]["filters"]["name_query"], "1080")
        self.assertIn("покоління", response["reply"])

    def test_explicit_generation_is_exact_and_does_not_match_v130(self):
        response = self.send("New Balance 1080 v13")
        self.assertEqual([item["name"] for item in response["products"]], ["Fresh Foam X 1080 v13"])
        self.assertIn("Fresh Foam X 1080 v13", response["reply"])
        self.assertFalse(response["needs_clarification"])

    def test_single_generation_is_named_exactly(self):
        self.products["v14"].active = False
        self.products["v130"].active = False
        self.db.commit()
        response = self.send("Fresh Foam X 1080")
        self.assertEqual(len(response["products"]), 1)
        self.assertIn("Fresh Foam X 1080 v13", response["reply"])
        self.assertNotIn("?", response["reply"])

    def test_absent_model_does_not_reuse_old_model_or_offer_unlabelled_alternative(self):
        self.send("New Balance 1080 v13")
        response = self.send("New Balance 1080 v99")
        self.assertEqual(response["products"], [])
        self.assertIn("відсутня", response["reply"])
        self.assertIn("v99", _sessions[self.sid]["filters"]["name_query"])
        self.assertNotIn("?", response["reply"])

    def test_new_model_replaces_only_model_conditions(self):
        self.send("New Balance 1080 v13 розмір 43 до $190")
        response = self.send("Fresh Foam X 1080 v14")
        self.assertEqual([item["name"] for item in response["products"]], ["Fresh Foam X 1080 v14"])
        self.assertEqual(response["filters"]["size"], 43)
        self.assertEqual(response["filters"]["max_price"], 190)
        self.assertNotIn("?", response["reply"])

    def test_missing_requested_size_does_not_replace_named_model(self):
        response = self.send("New Balance 1080 v13 розмір 44")
        self.assertEqual(response["products"], [])
        self.assertIn("відсутня", response["reply"])

    def test_catalog_facts_are_exact_including_stock_sku_and_sizes(self):
        response = self.send("New Balance 1080 v13")
        product = response["products"][0]
        self.assertEqual(product["price"], 160)
        self.assertEqual(product["total_stock"], 3)
        self.assertEqual(product["available_sizes"], [42.0, 43.0])
        self.assertEqual({item["sku"] for item in product["variants"]}, {"v13-42", "v13-43", "v13-44"})
        self.assertEqual(product["features"], [])
        self.assertTrue(all(item["stock"] in (0, 1, 2) for item in product["variants"]))

    def test_verified_model_table_maps_only_the_requested_measurement_kind(self):
        self.add_chart()
        response = self.send("New Balance 1080 v13 устілка 28 см")
        self.assertEqual(len(response["products"]), 1)
        self.assertEqual(response["products"][0]["matching_sizes"], [42.0])
        self.assertNotIn("size", response["filters"])
        self.assertIn("EU 42", response["products"][0]["match_reasons"][0])

    def test_foot_length_cannot_use_insole_or_cm_jp_column(self):
        self.add_chart()
        response = self.send("New Balance 1080 v13 стопа 28 см")
        self.assertEqual(response["products"], [])
        self.assertNotIn("size", response["filters"])

    def test_cm_jp_uses_its_own_verified_column(self):
        self.add_chart()
        response = self.send("New Balance 1080 v13 CM/JP 27.5")
        self.assertTrue(response["products"])
        self.assertEqual(response["products"][0]["matching_sizes"], [42.0, 43.0])
        self.assertNotIn(44.0, response["products"][0]["matching_sizes"])

    def test_unverified_chart_is_not_used(self):
        self.add_chart(verified="false")
        response = self.send("New Balance 1080 v13 устілка 28 см")
        self.assertEqual(response["products"], [])
        self.assertIn("підтвердити", response["reply"])

    def test_chart_of_another_generation_is_not_used(self):
        product = self.add_chart()
        row = next(item for item in product.attributes if item.attribute.code == "size_chart_model")
        row.value = "Fresh Foam X 1080 v14"
        self.db.commit()
        self.assertEqual(verified_measurement_sizes(product, {"kind": "insole_cm", "value": "28"}), [])

    def test_measurement_more_does_not_repeat_products(self):
        self.add_chart()
        first = self.send("New Balance 1080 v13 устілка 28 см")
        second = self.send("Покажи ещё")
        self.assertTrue(first["products"])
        self.assertEqual(second["products"], [])
        self.assertNotIn("?", second["reply"])

    def test_conflicting_budget_is_retained_until_explicit_correction(self):
        response = self.send("New Balance розмір 43 від 200 до 100")
        self.assertEqual(response["products"], [])
        self.assertEqual(response["filters"]["min_price"], 200)
        self.assertEqual(response["filters"]["max_price"], 100)
        self.assertEqual(response["reply"].count("?"), 1)
        corrected = self.send("до 300")
        self.assertEqual(corrected["filters"]["min_price"], 200)
        self.assertEqual(corrected["filters"]["max_price"], 300)
        self.assertTrue(corrected["products"])
        self.assertNotIn("?", corrected["reply"])

    def test_model_cannot_invent_eu_size_from_centimetres(self):
        payload = {"intent": "search_products", "filters": {"size": 44}, "evidence": {"size": "28 см"}}
        class Stub:
            def analyze(self, history, message):
                return payload
        with patch("app.services.chat.get_provider", return_value=Stub()):
            response = self.send("устілка 28 см")
        self.assertNotIn("size", response["filters"])
        self.assertEqual(response["products"], [])


class TestDatabaseGuardTests(unittest.TestCase):
    def test_tests_require_an_explicit_temporary_sqlite_file(self):
        with self.assertRaisesRegex(RuntimeError, "Set SNEAKCHAT_DATABASE_URL"):
            validate_test_database_url(None)
        with tempfile.TemporaryDirectory(prefix="sneakchat-db-guard-") as temp:
            url = "sqlite:///" + (Path(temp) / "isolated.sqlite").as_posix()
            self.assertEqual(validate_test_database_url(url), url)
        working_db = Path(__file__).resolve().parents[1] / "sneaker_store.db"
        with self.assertRaisesRegex(RuntimeError, "system temp directory"):
            validate_test_database_url("sqlite:///" + working_db.as_posix())


if __name__ == "__main__":
    unittest.main()

