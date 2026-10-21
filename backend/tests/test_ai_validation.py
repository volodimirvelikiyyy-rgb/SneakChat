"""Тесты ворот валидации AI-фильтров (`app.ai.validation`).

Проверяется не только факт отказа, но и его причина: молчаливое отклонение значения модели
так же опасно, как молчаливое принятие, потому что не позволяет отличить «каталог этого не
знает» от «проверка не сработала».
"""

import unittest

from app.ai.validation import (
    DEFAULT_INTENT,
    REASON_INVALID_INTENT,
    REASON_OUT_OF_RANGE,
    REASON_UNEXPECTED_KEY,
    REASON_UNKNOWN_ATTRIBUTE,
    REASON_UNKNOWN_VALUE,
    REASON_WRONG_TYPE,
    validate_analysis,
)
from app.database import SessionLocal, ensure_catalog_schema
from app.seed import seed_database


class ValidationGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def check(self, raw):
        return validate_analysis(self.db, raw)

    def reasons(self, raw):
        return [item.reason for item in self.check(raw).rejections]

    # --- типы -----------------------------------------------------------------

    def test_size_as_numeric_string_is_normalized(self):
        """Модель часто отдаёт число строкой: «43» однозначно и принимается как 43."""
        result = self.check({"intent": "search_products", "filters": {"size": "43"}})
        self.assertEqual(result.filters, {"size": 43.0})
        self.assertEqual(result.rejections, [])

    def test_size_with_words_is_rejected(self):
        result = self.check({"intent": "search_products", "filters": {"size": "43 размера"}})
        self.assertEqual(result.filters, {})
        self.assertIn(REASON_WRONG_TYPE, self.reasons({"intent": "search_products", "filters": {"size": "43 размера"}}))

    def test_textual_min_price_is_not_accepted_as_number(self):
        """«до 120» неизвестно, что ограничивает; такую фразу разбирает детерминированный парсер."""
        raw = {"intent": "search_products", "filters": {"min_price": "до 120"}}
        result = self.check(raw)
        self.assertNotIn("min_price", result.filters)
        self.assertEqual(self.reasons(raw), [REASON_WRONG_TYPE])

    def test_numeric_string_price_is_accepted(self):
        result = self.check({"intent": "search_products", "filters": {"max_price": "120"}})
        self.assertEqual(result.filters, {"max_price": 120.0})

    def test_out_of_range_size_is_rejected(self):
        raw = {"intent": "search_products", "filters": {"size": 99}}
        self.assertEqual(self.check(raw).filters, {})
        self.assertEqual(self.reasons(raw), [REASON_OUT_OF_RANGE])

    def test_negative_and_inverted_price_are_rejected(self):
        raw = {"intent": "search_products", "filters": {"min_price": -5}}
        self.assertEqual(self.reasons(raw), [REASON_OUT_OF_RANGE])
        inverted = {"intent": "search_products", "filters": {"min_price": 300, "max_price": 100}}
        result = self.check(inverted)
        self.assertNotIn("min_price", result.filters)
        self.assertNotIn("max_price", result.filters)
        self.assertEqual(self.reasons(inverted), [REASON_OUT_OF_RANGE])

    def test_size_is_not_validated_for_presence_only_for_type_and_axis(self):
        """Размера 46 в базе нет, но ось его допускает: отказ был бы подменой честного «нет в наличии»."""
        result = self.check({"intent": "search_products", "filters": {"size": 46}})
        self.assertEqual(result.filters, {"size": 46.0})
        self.assertEqual(result.rejections, [])

    # --- intent ---------------------------------------------------------------

    def test_null_intent_is_rejected_and_breaks_the_contract(self):
        raw = {"intent": None, "filters": {}}
        result = self.check(raw)
        self.assertEqual(result.intent, DEFAULT_INTENT)
        self.assertEqual(self.reasons(raw), [REASON_INVALID_INTENT])
        self.assertTrue(result.contract_broken)

    def test_unknown_intent_is_rejected_and_breaks_the_contract(self):
        raw = {"intent": "delete_everything", "filters": {}}
        result = self.check(raw)
        self.assertEqual(result.intent, DEFAULT_INTENT)
        self.assertIn(REASON_INVALID_INTENT, self.reasons(raw))
        self.assertTrue(result.contract_broken)

    def test_valid_intent_keeps_the_contract(self):
        result = self.check({"intent": "recommendation", "filters": {}})
        self.assertEqual(result.intent, "recommendation")
        self.assertFalse(result.contract_broken)

    # --- контракт ответа ------------------------------------------------------

    def test_unknown_top_level_key_breaks_the_contract(self):
        raw = {"intent": "search_products", "filters": {}, "reply": "эти Nike водонепроницаемые"}
        result = self.check(raw)
        self.assertIn(REASON_UNEXPECTED_KEY, self.reasons(raw))
        self.assertTrue(result.contract_broken)

    def test_extra_product_fact_field_breaks_the_contract(self):
        raw = {"intent": "search_products", "filters": {}, "price": 1}
        self.assertTrue(self.check(raw).contract_broken)

    def test_empty_object_is_safe_but_violates_the_contract(self):
        """Разбора нет вовсе: фильтров не появится, но и доверия такому ответу нет."""
        result = self.check({})
        self.assertEqual(result.filters, {})
        self.assertEqual(result.intent, DEFAULT_INTENT)
        self.assertIn(REASON_INVALID_INTENT, [item.reason for item in result.rejections])
        self.assertTrue(result.contract_broken)

    def test_non_dict_analysis_is_rejected(self):
        result = validate_analysis(self.db, ["search_products"])
        self.assertEqual(result.filters, {})
        self.assertEqual([item.reason for item in result.rejections], [REASON_WRONG_TYPE])

    # --- атрибуты -------------------------------------------------------------

    def test_unknown_attribute_code_is_rejected(self):
        raw = {"intent": "search_products", "filters": {"attributes": {"waterproof": "yes"}}}
        result = self.check(raw)
        self.assertNotIn("attributes", result.filters)
        self.assertEqual(self.reasons(raw), [REASON_UNKNOWN_ATTRIBUTE])

    def test_unknown_attribute_value_is_rejected(self):
        raw = {"intent": "search_products", "filters": {"attributes": {"color": "Purple"}}}
        self.assertEqual(self.reasons(raw), [REASON_UNKNOWN_VALUE])

    def test_numeric_attribute_is_accepted(self):
        result = self.check({"intent": "search_products", "filters": {"attributes": {"size": "43"}}})
        self.assertEqual(result.filters, {"attributes": {"size": "43"}})

    def test_attribute_type_must_be_an_object(self):
        raw = {"intent": "search_products", "filters": {"attributes": ["color", "White"]}}
        self.assertEqual(self.reasons(raw), [REASON_WRONG_TYPE])

    def test_numeric_attribute_is_checked_against_the_axis_bounds(self):
        """size=99 отклоняется и как прямой ключ, и как атрибут: иначе он доходит до SQL."""
        raw = {"intent": "search_products", "filters": {"attributes": {"size": "99"}}}
        result = self.check(raw)
        self.assertNotIn("attributes", result.filters)
        self.assertEqual(self.reasons(raw), [REASON_OUT_OF_RANGE])

    def test_numeric_attribute_inside_the_axis_bounds_is_accepted(self):
        result = self.check({"intent": "search_products", "filters": {"attributes": {"size": "43"}}})
        self.assertEqual(result.filters, {"attributes": {"size": "43"}})

    def test_attribute_valued_by_another_axis_value_is_rejected(self):
        """Код объявлен, но значение ему не принадлежит: размер не бывает белым."""
        raw = {"intent": "search_products", "filters": {"attributes": {"size": "White"}}}
        self.assertEqual(self.reasons(raw), [REASON_WRONG_TYPE])

    # --- категориальные значения в живом каталоге -----------------------------

    def test_value_present_in_database_is_accepted(self):
        result = self.check({"intent": "search_products", "filters": {"brand": "Nike", "color": "White"}})
        self.assertEqual(result.filters, {"brand": "Nike", "color": "White"})
        self.assertEqual(result.rejections, [])

    def test_user_wording_is_canonicalised_against_the_database(self):
        result = self.check({"intent": "search_products", "filters": {"color": "gray", "brand": "найк"}})
        self.assertEqual(result.filters, {"brand": "Nike", "color": "Grey"})

    def test_unknown_brand_is_rejected(self):
        raw = {"intent": "search_products", "filters": {"brand": "Reebok"}}
        result = self.check(raw)
        self.assertEqual(result.filters, {})
        self.assertEqual(self.reasons(raw), [REASON_UNKNOWN_VALUE])

    def test_unknown_use_case_is_rejected(self):
        raw = {"intent": "search_products", "filters": {"use_case": "баскетбол"}}
        self.assertEqual(self.reasons(raw), [REASON_UNKNOWN_VALUE])

    def test_known_use_case_word_is_accepted(self):
        result = self.check({"intent": "search_products", "filters": {"use_case": "бег"}})
        self.assertEqual(result.filters, {"use_case": "Running"})

    def test_all_catalogue_values_pass_validation(self):
        """Ни одно реальное значение каталога не должно отклоняться воротами."""
        from app.catalog import catalogue_brands, declared_attribute_values
        from app.catalog_rules import SNEAKER_CONFIG

        live = declared_attribute_values(self.db, SNEAKER_CONFIG)
        for brand in catalogue_brands(self.db):
            with self.subTest(brand=brand):
                self.assertEqual(self.check({"intent": "search_products", "filters": {"brand": brand}}).filters, {"brand": brand})
        for code, values in live.items():
            for value in values:
                with self.subTest(code=code, value=value):
                    result = self.check({"intent": "search_products", "filters": {"attributes": {code: value}}})
                    self.assertNotIn(REASON_UNKNOWN_VALUE, [item.reason for item in result.rejections])

    # --- без базы -------------------------------------------------------------

    def test_without_database_categorical_values_are_not_dropped(self):
        """Нет данных для сверки — значит и нет основания отказывать."""
        result = validate_analysis(None, {"intent": "search_products", "filters": {"brand": "Nike", "color": "White"}})
        self.assertEqual(result.filters, {"brand": "Nike", "color": "White"})
        self.assertEqual(result.rejections, [])

    def test_rejections_are_described_in_text(self):
        result = self.check({"intent": "bad", "filters": {"brand": "Reebok"}})
        self.assertTrue(result.rejected)
        self.assertTrue(all(isinstance(item, str) and item for item in result.rejected))


if __name__ == "__main__":
    unittest.main()
