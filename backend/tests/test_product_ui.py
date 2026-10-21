"""Тесты Этапа 6: то, что покупатель видит в интерфейсе, обязано быть правдой.

Здесь проверяются не новые механики, а честность уже существующих: кнопка «подешевле»
объясняет результат, честное «нет» называет реальный рычаг и реальные размеры, широкое
ослабление не обещает «без ограничений» рядом с сохранёнными условиями, а подсказки
интерфейса действительно меняют запрос, а не повторяют прежний ответ.
"""

import re
import unittest

from app.ai.grounding import verify_against_filters, verify_reasons, verify_reply
from app.database import SessionLocal, ensure_catalog_schema
from app.seed import seed_database
from app.services.chat import _sessions, chat
from app.services.products import find_products, serialize_product


class ProductUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def conversation(self, *messages, session=None, language="ru"):
        name = session or f"ui-{messages[0][:20]}-{language}"
        _sessions.pop(name, None)
        return [chat(self.db, name, message, language) for message in messages]

    def assert_grounded(self, result, filters=None):
        """Ни одно утверждение ответа не должно расходиться с базой."""
        conditions = filters if filters is not None else (result.get("filters") or {})
        problems = verify_reply(result["reply"], result["products"], conditions)
        problems += verify_reasons(result["products"])
        for product in result["products"]:
            problems += verify_against_filters(product, conditions, result["reply"])
        self.assertEqual(problems, [], f"ответ не подтверждён базой: {problems}")

    # 1. «Покажи подешевле» без более дешёвых моделей: ответ объясняет, а не повторяется.
    def test_cheaper_without_cheaper_options_explains_itself(self):
        results = self.conversation("Adidas Superstar 43", "покажи дешевле", session="ui-cheaper-none")
        first, second = results
        self.assertTrue(first["products"])
        self.assertFalse(second["products"])
        self.assertNotEqual(first["reply"], second["reply"], "кнопка «дешевле» не должна повторять прежний ответ")
        self.assertIn("Дешевле показанного", second["reply"])
        # Новых сумм, которых нет ни у товара, ни в условиях, в пояснении нет.
        self.assert_grounded(second)

    # 2. «Подешевле» с реально более дешёвыми моделями: выдача меняется и остаётся в бюджете.
    def test_cheaper_with_options_shows_cheaper_products(self):
        results = self.conversation("Adidas Ultraboost Light 43", "покажи дешевле", session="ui-cheaper-ok")
        first, second = results
        self.assertTrue(second["products"])
        cheapest_before = min(product["price"] for product in first["products"])
        self.assertTrue(all(product["price"] < cheapest_before for product in second["products"]))
        self.assertLess(second["filters"]["max_price"], cheapest_before)
        self.assert_grounded(second)

    # 3. Честное «нет» называет реальный рычаг, а не «один из фильтров».
    def test_no_match_names_the_real_lever(self):
        results = self.conversation("Nike размер 46 до 80", session="ui-no-match")
        result = results[0]
        self.assertEqual(result["products"], [])
        self.assertIn("бренд", result["reply"])
        self.assertIn("бюджет", result["reply"])
        self.assertIn("размер", result["reply"])
        self.assertNotIn("один из фильтров", result["reply"])

    # 4. Вместо несуществующего размера называются размеры, которые действительно есть.
    def test_no_match_shows_really_available_sizes(self):
        results = self.conversation("Nike размер 46", session="ui-size-options")
        result = results[0]
        self.assertIn("Ближайшие доступные размеры", result["reply"])
        tail = result["reply"].split("Ближайшие доступные размеры:")[1].split(".")[0]
        listed = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", tail)]
        self.assertTrue(listed)
        # Каждый названный размер обязан быть у товара Nike в наличии.
        stocks = {
            float(size)
            for product in find_products(self.db, brand="Nike")
            for size in serialize_product(product)["available_sizes"]
        }
        self.assertTrue(set(listed).issubset(stocks), f"{listed} не подтверждены каталогом {sorted(stocks)}")

    # 5. Подсказки честного «нет» — рабочие команды, а не декорация.
    def test_no_match_suggestions_change_the_request(self):
        results = self.conversation("Nike размер 46 до 80", session="ui-levers")
        suggestions = results[0]["suggestions"]
        self.assertTrue(suggestions)
        for suggestion in suggestions:
            follow_up = self.conversation("Nike размер 46 до 80", suggestion, session=f"ui-lever-{suggestion[:10]}")
            before = results[0]["filters"]
            after = follow_up[1]["filters"]
            self.assertNotEqual(before, after, f"подсказка «{suggestion}» не меняет условия запроса")

    # 6. Широкое ослабление не обещает противоречивого.
    def test_broadest_fallback_is_not_self_contradictory(self):
        results = self.conversation("Nike 43 голубой до 80", session="ui-broadest")
        reply = results[0]["reply"]
        self.assertFalse(results[0]["products"])
        self.assertEqual(results[0]["filters"]["max_price"], 80.0)
        self.assertIn("бюджет", reply)
        self.assertNotIn("без ограничений", reply)
        self.assert_grounded(results[0])

    # 7. То же поведение во втором языке интерфейса.
    def test_ukrainian_no_match_is_equally_explicit(self):
        results = self.conversation("Nike розмір 46", language="uk", session="ui-uk")
        result = results[0]
        self.assertIn("Найближчі доступні розміри", result["reply"])
        self.assertIn("Можна послабити: бренд або розмір", result["reply"])
        self.assertEqual(result["suggestions"], ["Бренд не важливий", "Розмір не враховуй"])
        self.assert_grounded(result)

    # 8. Пояснение причины остаётся привязанным к условиям запроса.
    def test_reasons_are_confirmed_by_catalogue(self):
        results = self.conversation("Nike 43 голубой до 80", session="ui-reasons")
        for product in results[0]["products"]:
            self.assertTrue(product["match_reasons"])
            self.assert_grounded(results[0])

    # 9. Явный ценовой потолок нельзя пересекать при поиске альтернатив.
    def test_hard_budget_is_never_relaxed(self):
        near = self.conversation("Ищу Nike для бега 43 до 120", session="ui-over-budget-near")[0]
        self.assertTrue(all(product["price"] <= near["filters"]["max_price"] for product in near["products"]))

        wide = self.conversation("Nike 43 голубые до $80", session="ui-over-budget-wide")[0]
        self.assertFalse(wide["products"])
        self.assertEqual(wide["filters"]["max_price"], 80.0)


if __name__ == "__main__":
    unittest.main()
