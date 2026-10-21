"""Тесты `app.ai.grounding`.

Модуль играет две роли, и обе проверяются:

* runtime-страховка — `_guard_response` вызывается в `chat()` перед отправкой ответа и не
  выпускает наружу то, что не подтверждено базой;
* независимый verifier — те же функции используются тестами, чтобы доказать отсутствие
  выдуманных фактов в ответах.
"""

import unittest

from app.ai.grounding import allowed_amounts, reply_amounts, verify_products, verify_reasons, verify_reply
from app.database import SessionLocal, ensure_catalog_schema
from app.seed import seed_database
from app.services.chat import _guard_response, _sessions, chat
from app.services.products import get_product, serialize_product


class GroundingVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)
        cls.product = serialize_product(get_product(cls.db, 1))
        cls.product["match_reasons"] = ["есть в наличии"]

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def mutated(self, **changes):
        product = {**self.product, **changes}
        product["match_reasons"] = ["есть в наличии"]
        return product

    # --- факты товара ---------------------------------------------------------

    def test_grounded_product_has_no_problems(self):
        self.assertEqual(verify_products(self.db, [self.product]), [])
        self.assertEqual(verify_reasons([self.product]), [])

    def test_invented_price_is_detected(self):
        problems = verify_products(self.db, [self.mutated(price=self.product["price"] - 1)])
        self.assertTrue(any("price" in problem for problem in problems))

    def test_wrong_availability_is_detected(self):
        problems = verify_products(self.db, [self.mutated(total_stock=self.product["total_stock"] + 10)])
        self.assertTrue(any("total_stock" in problem for problem in problems))

    def test_wrong_size_list_is_detected(self):
        problems = verify_products(self.db, [self.mutated(available_sizes=self.product["available_sizes"] + [99.0])])
        self.assertTrue(any("available_sizes" in problem for problem in problems))

    def test_wrong_colour_is_detected(self):
        problems = verify_products(self.db, [self.mutated(color="Purple")])
        self.assertTrue(any("color" in problem for problem in problems))

    def test_unconfirmed_property_is_detected(self):
        problems = verify_products(self.db, [self.mutated(features=["waterproof"])])
        self.assertTrue(any("features" in problem for problem in problems))

    def test_invented_name_is_detected(self):
        problems = verify_products(self.db, [self.mutated(name="Air Max 9999")])
        self.assertTrue(any("name" in problem for problem in problems))

    def test_missing_product_is_detected(self):
        problems = verify_products(self.db, [self.mutated(id=10 ** 6)])
        self.assertTrue(any("отсутствует" in problem for problem in problems))

    def test_product_without_reason_is_detected(self):
        self.assertTrue(verify_reasons([{**self.product, "match_reasons": []}]))

    # --- суммы в тексте -------------------------------------------------------

    def test_amounts_are_read_from_the_reply(self):
        self.assertEqual(reply_amounts("Ниже — 3 до $120."), {120.0})
        self.assertEqual(reply_amounts("Ничего не найдено."), set())

    def test_product_price_confirms_an_amount(self):
        product = self.mutated(price=99.0)
        # Цена в тексте должна совпадать с ценой товара в базе, поэтому берём реальную.
        self.assertEqual(verify_reply(f"Цена ${self.product['price']:g}.", [self.product], {}), [])

    def test_unconfirmed_amount_is_detected(self):
        self.assertEqual(verify_reply("Цена $1.", [self.product], {}), ["неподтверждённая сумма $1"])

    def test_filter_bound_confirms_an_amount(self):
        self.assertEqual(verify_reply("Ниже — до $120.", [self.product], {"max_price": 120.0}), [])

    def test_word_currency_amounts_are_checked_too(self):
        """Неподтверждённая сумма блокируется и в записи словом валюты."""
        for text in ("Сумма 999 долларов.", "Итого 1 доллар.", "Цена 777 грн."):
            with self.subTest(text=text):
                self.assertTrue(verify_reply(text, [self.product], {}))

    def test_confirmed_word_currency_amount_passes(self):
        price = f"{self.product['price']:g}"
        self.assertEqual(verify_reply(f"Цена {price} долларов.", [self.product], {}), [])

    def test_grounded_response_has_no_violations(self):
        self.assertEqual(verify_reply("Есть в наличии.", [self.product], {}), [])
        self.assertEqual(allowed_amounts([self.product], {"min_price": 80.0}), {self.product["price"], 80.0})


class RuntimeGuardTests(unittest.TestCase):
    """Страховка в реальном пути ответа: неподтверждённое наружу не выходит."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def real_product(self):
        product = serialize_product(get_product(self.db, 1))
        product["match_reasons"] = ["есть в наличии"]
        return product

    def test_invented_product_is_dropped_from_the_response(self):
        result = {"reply": "Нашёл.", "products": [{**self.real_product(), "price": 1.0}], "needs_clarification": False}
        guarded = _guard_response(self.db, result, {})
        self.assertEqual(guarded["products"], [])
        self.assertNotIn("Нашёл", guarded["reply"])
        self.assertTrue(guarded["needs_clarification"])

    def test_only_unconfirmed_products_are_dropped(self):
        good = self.real_product()
        bad = {**good, "id": good["id"], "features": ["waterproof"]}
        result = {"reply": "Нашёл.", "products": [bad, good], "needs_clarification": False}
        guarded = _guard_response(self.db, result, {})
        self.assertEqual(len(guarded["products"]), 1)

    def test_unconfirmed_amount_rebuilds_the_reply(self):
        result = {"reply": "Цена $1.", "products": [self.real_product()], "needs_clarification": False}
        guarded = _guard_response(self.db, result, {}, "ru")
        self.assertNotIn("$1", guarded["reply"])
        self.assertTrue(guarded["reply"])

    def test_grounded_response_is_returned_unchanged(self):
        product = self.real_product()
        result = {"reply": "Показываю модели из каталога: 1.", "products": [product], "needs_clarification": False}
        guarded = _guard_response(self.db, result, {})
        self.assertEqual(guarded["reply"], result["reply"])
        self.assertEqual(len(guarded["products"]), 1)

    def test_guard_runs_on_the_real_chat_path(self):
        """Ответ обычного диалога обязан проходить проверку без замечаний."""
        _sessions.pop("guard-live", None)
        result = chat(self.db, "guard-live", "Nike 43")
        self.assertEqual(verify_products(self.db, result["products"]), [])
        self.assertEqual(verify_reasons(result["products"]), [])
        self.assertEqual(verify_reply(result["reply"], result["products"], _sessions["guard-live"]["filters"]), [])

    def test_guard_covers_multi_turn_replies(self):
        _sessions.pop("guard-multi", None)
        for message in ("для бега 42", "не Nike", "до 100", "покажи ещё", "покажи похожие"):
            result = chat(self.db, "guard-multi", message)
            with self.subTest(message=message):
                self.assertEqual(verify_products(self.db, result["products"]), [])
                self.assertEqual(verify_reply(result["reply"], result["products"], _sessions["guard-multi"]["filters"]), [])

    def test_guard_is_actually_wired_into_chat(self):
        """Проверка, что страховка вызывается: подменяем её и следим за вызовом."""
        from unittest.mock import patch

        calls = []
        original = __import__("app.services.chat", fromlist=["_guard_response"])._guard_response

        def spy(db, result, filters, language="ru"):
            calls.append(result.get("reply"))
            return original(db, result, filters, language)

        _sessions.pop("guard-wired", None)
        with patch("app.services.chat._guard_response", side_effect=spy):
            chat(self.db, "guard-wired", "Nike 43")
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
