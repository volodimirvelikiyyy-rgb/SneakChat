"""Тесты отрицаний: «не Nike», «белые не нужны», «не для бега», «не 43».

Отдельно проверяются ложные срабатывания: ценовая формулировка не должна превращаться в
исключение размера, а «чтобы не промокали» — в отказ от свойства.
"""

import unittest
from unittest.mock import patch

from app.catalog_rules import (detect_absent_properties, detect_negations, detect_removals,
                              negation_mask)
from app.database import SessionLocal, ensure_catalog_schema
from app.seed import seed_database
from app.services.assistant import parse_request
from app.services.chat import _sessions, chat


class NegationDetectionTests(unittest.TestCase):
    """Распознавание: маркер перед значением и фраза отказа после него."""

    def test_marker_before_value(self):
        for message, expected in (
            ("не Nike", {"brand": ["Nike"]}),
            ("только не Nike", {"brand": ["Nike"]}),
            ("не белые", {"color": ["White"]}),
            ("не для бега", {"use_case": ["Running"]}),
            ("не 43", {"size": [43.0]}),
            ("без белого", {"color": ["White"]}),
            ("без защиты от влаги", {"feature": ["water_resistant"]}),
            ("хочу не адидас", {"brand": ["Adidas"]}),
        ):
            with self.subTest(message=message):
                self.assertEqual(detect_negations(message), expected)

    def test_rejection_phrase_after_value(self):
        for message, expected in (
            ("Nike не нужен", {"brand": ["Nike"]}),
            ("белые не нужны", {"color": ["White"]}),
            ("для бега не подходит", {"use_case": ["Running"]}),
            ("43 не нужен", {"size": [43.0]}),
            ("белых не надо", {"color": ["White"]}),
        ):
            with self.subTest(message=message):
                self.assertEqual(detect_negations(message), expected)

    def test_several_negations_in_one_message(self):
        self.assertEqual(detect_negations("не Nike и не белые"), {"brand": ["Nike"], "color": ["White"]})

    def test_negation_is_removed_from_the_positive_parse(self):
        """«только не Nike» — не фильтр по Nike, иначе пользователю покажут отвергнутое."""
        self.assertEqual(parse_request("Кроссовки размер 43, только не Nike"), {"size": 43.0})
        self.assertEqual(parse_request("не белые, размер 43"), {"size": 43.0})
        self.assertNotIn("brand", parse_request("Nike не нужен"))

    def test_value_named_outside_the_negation_stays_positive(self):
        """«Nike, но не Nike» — настоящее противоречие: положительное условие остаётся."""
        self.assertEqual(parse_request("Nike, но не Nike, размер 43"), {"brand": "Nike", "size": 43.0})


class NegationFalsePositiveTests(unittest.TestCase):
    """Цена и свойство не должны читаться как отрицание размера или признака."""

    def test_price_phrases_are_not_size_negations(self):
        for message in ("не дороже 43", "не больше 100", "не менее 80", "не хочу дороже 120", "до 150", "размер 43, не дороже 100"):
            with self.subTest(message=message):
                self.assertEqual(detect_negations(message), {})

    def test_price_phrases_still_parse_as_budget(self):
        self.assertEqual(parse_request("размер 43, не дороже 100"), {"max_price": 100.0, "size": 43.0})
        self.assertEqual(parse_request("не больше 100"), {"max_price": 100.0})

    def test_water_resistance_request_is_not_a_negation(self):
        """«чтобы не промокали» — запрос свойства, а не отказ от него."""
        for message in ("чтобы не промокали на осень", "не промокают, размер 43", "водонепроницаемые, 43"):
            with self.subTest(message=message):
                self.assertEqual(detect_negations(message), {})

    def test_water_resistance_still_parses_as_a_feature(self):
        filters = parse_request("чтобы не промокали на осень")
        self.assertEqual(filters["feature"], ["autumn", "water_resistant"])

    def test_hesitation_words_do_not_create_negations(self):
        self.assertEqual(detect_negations("не знаю, для бега или для зала"), {})

    def test_negation_does_not_cross_punctuation(self):
        self.assertEqual(detect_negations("это не важно. Покажи белые"), {})

    def test_non_refusal_sentences_are_ignored(self):
        self.assertEqual(detect_negations("недорого"), {})
        self.assertEqual(detect_negations("покажи белые"), {})

    def test_mask_keeps_unrelated_conditions(self):
        """Маскируется только исключённое значение, остальные условия остаются."""
        self.assertEqual(parse_request("Чёрные не нужны 43"), {"size": 43.0})
        self.assertEqual(parse_request("белые не нужны, размер 43"), {"size": 43.0})


class EvidenceGroundingTests(unittest.TestCase):
    """Модель не может навязать значение, подтвердив его произвольным словом сообщения."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def run_with(self, payload, message, session):
        class FakeProvider:
            def analyze(self, history, text):
                return payload

        _sessions.pop(session, None)
        with patch("app.services.chat.get_provider", return_value=FakeProvider()):
            result = chat(self.db, session, message)
        return result, dict(_sessions[session]["filters"])

    def test_generic_word_cannot_confirm_an_invented_brand(self):
        """«кроссовки» не подтверждает бренд Puma: значения «Puma» в сообщении нет."""
        payload = {"intent": "search_products", "filters": {"brand": "Puma"}, "evidence": {"brand": "кроссовки"}}
        result, filters = self.run_with(payload, "покажи кроссовки 43", "evidence-brand")
        self.assertNotIn("brand", filters)
        self.assertEqual(result["mode"], "fallback")

    def test_generic_word_cannot_confirm_an_invented_colour(self):
        payload = {"intent": "search_products", "filters": {"color": "Black"}, "evidence": {"color": "покажи"}}
        _, filters = self.run_with(payload, "покажи что-нибудь", "evidence-color")
        self.assertNotIn("color", filters)

    def test_wrong_dictionary_word_is_rejected(self):
        """Словарь знает слово «белые», но оно про цвет, а не про бренд."""
        payload = {"intent": "search_products", "filters": {"brand": "Nike"}, "evidence": {"brand": "белые"}}
        _, filters = self.run_with(payload, "белые кроссовки 43", "evidence-owners")
        self.assertNotIn("brand", filters)

    def test_dictionary_word_pointing_to_another_value_is_rejected(self):
        """«белые» указывает на White, а не на Red: значение модели не применяется.

        Цвет в условиях при этом есть — но от детерминированного парсера, который прочитал
        слово «белые» сам. Проверяется именно то, что модель не подменила значение.
        """
        payload = {"intent": "search_products", "filters": {"color": "Red"}, "evidence": {"color": "белые"}}
        _, filters = self.run_with(payload, "белые кроссовки 43", "evidence-value")
        self.assertNotEqual(filters.get("color"), "Red")
        self.assertEqual(filters.get("color"), "White")

    def test_trusted_wording_still_adds_the_value(self):
        """Формулировка из доверенного списка работает: «тёмные» → Black."""
        payload = {"intent": "search_products", "filters": {"color": "Black"}, "evidence": {"color": "тёмные"}}
        result, filters = self.run_with(payload, "Хочу тёмные кроссовки, размер 43", "evidence-trusted")
        self.assertEqual(filters.get("color"), "Black")
        self.assertEqual(result["mode"], "ollama")


class AuditRoundTwoTests(unittest.TestCase):
    """Регрессии на дефекты, найденные повторным аудитом (D1–D4)."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def run_with(self, payload, message, session):
        class FakeProvider:
            def analyze(self, history, text):
                return payload

        _sessions.pop(session, None)
        with patch("app.services.chat.get_provider", return_value=FakeProvider()):
            result = chat(self.db, session, message)
        return result, dict(_sessions[session]["filters"])

    def test_borrowed_number_does_not_confirm_a_size(self):
        """D1: цена 120 не подтверждает размер 44."""
        from app.services.chat import _grounded_ai_filters

        self.assertEqual(_grounded_ai_filters({"size": 44}, {"size": "120"}, "хочу Nike до $120"), {})
        self.assertEqual(_grounded_ai_filters({"attributes": {"size": 50}}, {"size": "120"}, "хочу Nike до $120"), {})
        self.assertEqual(_grounded_ai_filters({"max_price": 120}, {"max_price": "120"}, "хочу Nike до $120"), {"max_price": 120})

    def test_borrowed_number_does_not_reach_the_session(self):
        """D1: навязанный размер не доходит ни до условий, ни до выдачи."""
        payload = {"intent": "search_products", "filters": {"size": 44}, "evidence": {"size": "120"}}
        _, filters = self.run_with(payload, "хочу Nike до $120", "d1-session")
        self.assertNotIn("size", filters)

    def test_borrowed_number_does_not_confirm_a_numeric_attribute(self):
        """D1: тот же путь для attributes.size."""
        payload = {"intent": "search_products", "filters": {"attributes": {"size": 50}}, "evidence": {"size": "120"}}
        _, filters = self.run_with(payload, "хочу Nike до $120", "d1-attr")
        self.assertNotIn("attributes", filters)

    def test_trusted_wording_works_inside_a_phrase(self):
        """D2: «для пробежек» — доверенная формулировка, хотя фраза начинается со служебного слова."""
        payload = {"intent": "search_products", "filters": {"use_case": "Running"}, "evidence": {"use_case": "для пробежек"}}
        _, filters = self.run_with(payload, "нужны для пробежек 43", "d2-phrase")
        self.assertEqual(filters.get("use_case"), "Running")

    def test_inflected_brand_is_recognised(self):
        """D2: падеж бренда — обычная пользовательская форма."""
        self.assertEqual(parse_request("покажи пумы 43"), {"brand": "Puma", "size": 43.0})

    def test_foreign_word_is_not_a_colour(self):
        """D3: короткие основы больше не матчатся внутри посторонних слов."""
        for word in ("сертификат", "серия", "синица", "оливье", "беженец", "кремль"):
            with self.subTest(word=word):
                self.assertNotIn("color", parse_request(f"нужен {word} 43"))

    def test_legitimate_colour_forms_still_work(self):
        """D3: полные формы цветов продолжают работать."""
        for word, expected in (("серые", "Grey"), ("синие", "Blue"), ("оливковые", "Olive"), ("зелёные", "Green"), ("бежевые", "Beige")):
            with self.subTest(word=word):
                self.assertEqual(parse_request(f"{word} 43").get("color"), expected)

    def test_product_contradicting_the_stated_condition_is_dropped(self):
        """D4: товар, не отвечающий названному условию, не показывается."""
        from app.ai.grounding import verify_against_filters

        adidas = {"id": 1, "brand": "Adidas"}
        self.assertTrue(verify_against_filters(adidas, {"brand": "Nike"}, "Нашёл вариант по условиям «Nike»"))
        self.assertEqual(verify_against_filters(adidas, {"brand": "Nike"}, "Ниже — 1 другого бренда"), [])
        self.assertTrue(verify_against_filters({"id": 2, "brand": "Nike"}, {"exclude_brand": ["Nike"]}, ""))

    def test_guard_uses_the_filter_consistency_check(self):
        """D4: проверка соответствия подключена к runtime-страховке.

        Товар настоящий и совпадает с базой, поэтому решение может принять только проверка
        условий: без неё он был бы показан.
        """
        from app.ai.grounding import verify_products
        from app.services.chat import _guard_response
        from app.services.products import get_product, serialize_product

        nike = serialize_product(get_product(self.db, 1))
        self.assertEqual(nike["brand"], "Nike")
        nike["match_reasons"] = ["есть в наличии"]
        self.assertEqual(verify_products(self.db, [nike]), [])

        kept = _guard_response(self.db, {"reply": "Нашёл.", "products": [nike], "needs_clarification": False}, {})
        self.assertEqual(len(kept["products"]), 1, "без исключения товар обязан остаться")

        dropped = _guard_response(
            self.db,
            {"reply": "Нашёл.", "products": [nike], "needs_clarification": False},
            {"exclude_brand": ["Nike"]},
        )
        self.assertEqual(dropped["products"], [], "исключённый бренд не показывается")


class AuditRoundThreeTests(unittest.TestCase):
    """Регрессии на остатки, найденные третьим аудитом (украинские формы, посторонние слова, ось числа)."""

    def test_ukrainian_use_case_wording_is_accepted(self):
        """«пробіжок»/«пробіжки» — законные формы, они принимаются от модели."""
        from app.services.chat import _grounded_ai_filters

        for word in ("пробіжок", "пробіжки"):
            with self.subTest(word=word):
                self.assertEqual(
                    _grounded_ai_filters({"use_case": "Running"}, {"use_case": word}, f"для {word} 43"),
                    {"use_case": "Running"},
                )

    def test_foreign_words_are_not_colours(self):
        """Слова, начинающиеся с цветовой основы, цветом не являются."""
        for word in ("белка", "белок", "черновик", "черника", "червонец", "оранжерея", "сертификат",
                     "серия", "синица", "оливье", "беженец", "кремль"):
            with self.subTest(word=word):
                self.assertNotIn("color", parse_request(f"{word} 43"))

    def test_colour_forms_still_work(self):
        for word, expected in (("белые", "White"), ("белый", "White"), ("чёрные", "Black"),
                               ("красные", "Red"), ("оранжевые", "Orange"), ("серые", "Grey"),
                               ("синие", "Blue"), ("зелёные", "Green"), ("бежевые", "Beige")):
            with self.subTest(word=word):
                self.assertEqual(parse_request(f"{word} 43").get("color"), expected)

    def test_model_value_stem_is_still_canonicalised(self):
        """Значение модели — отдельное слово, поэтому основа принимается."""
        from app.services.chat import _normalize_ai_filters

        self.assertEqual(_normalize_ai_filters({"color": "бел"}), {"color": "White"})
        self.assertEqual(_normalize_ai_filters({"color": "хаки-2"}), {})

    def test_number_confirms_only_its_own_axis(self):
        """Число подтверждает значение, только если фраза говорит о той же оси."""
        from app.services.chat import _grounded_ai_filters

        self.assertEqual(_grounded_ai_filters({"max_price": 3}, {"max_price": "3 пары"}, "хочу 3 пары кроссовок"), {})
        self.assertEqual(_grounded_ai_filters({"min_price": 120}, {"min_price": "до 120"}, "кроссовки до 120"), {})
        self.assertEqual(_grounded_ai_filters({"max_price": 120}, {"max_price": "до 120"}, "кроссовки до 120"), {"max_price": 120})
        self.assertEqual(_grounded_ai_filters({"size": 43}, {"size": "размер 43"}, "размер 43"), {"size": 43})

    def test_numeric_size_exclusion_is_verified(self):
        from app.ai.grounding import verify_against_filters

        product = {"id": 1, "brand": "X", "available_sizes": [43.0]}
        self.assertTrue(verify_against_filters(product, {"exclude_attributes": {"size": [43]}}, ""))
        self.assertEqual(verify_against_filters(product, {"exclude_attributes": {"size": [44]}}, ""), [])


class AgeIsNotSizeTests(unittest.TestCase):
    """Возраст человека не становится размером, а ответ на вопрос о размере — становится."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_age_phrases_are_not_sizes(self):
        for message in ("мне 43 года", "мне 43", "43 роки", "мені 43 роки", "Мне 43 года, нужны кроссовки",
                        "мне 30 лет", "мені 30 років"):
            with self.subTest(message=message):
                self.assertNotIn("size", parse_request(message))

    def test_size_phrases_still_work(self):
        for message, expected in (("размер 43", 43.0), ("43", 43.0), ("нужен 43 размер", 43.0),
                                  ("43 размер", 43.0), ("до 120, размер 43", 43.0)):
            with self.subTest(message=message):
                self.assertEqual(parse_request(message).get("size"), expected)

    def test_age_does_not_hide_another_real_size(self):
        self.assertEqual(parse_request("мне 30 лет, размер 43"), {"size": 43.0})

    def test_answer_to_the_size_question_is_still_a_size(self):
        _sessions.pop("age-dialog", None)
        chat(self.db, "age-dialog", "нужны кроссовки")
        result = chat(self.db, "age-dialog", "43")
        self.assertEqual(_sessions["age-dialog"]["filters"].get("size"), 43.0)
        self.assertTrue(result["products"])

    def test_age_cannot_confirm_a_size_from_the_model(self):
        """Модель тоже не может превратить возраст в размер."""
        from app.services.chat import _grounded_ai_filters

        self.assertEqual(_grounded_ai_filters({"size": 43}, {"size": "43"}, "мне 43 года"), {})
        self.assertEqual(_grounded_ai_filters({"attributes": {"size": 43}}, {"size": "43"}, "мені 43 роки"), {})
        self.assertEqual(_grounded_ai_filters({"size": 43}, {"size": "43"}, "размер 43"), {"size": 43})

    def test_age_does_not_become_a_size_in_the_dialog(self):
        class AgeProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"size": 43}, "evidence": {"size": "43"}}

        _sessions.pop("age-session", None)
        with patch("app.services.chat.get_provider", return_value=AgeProvider()):
            chat(self.db, "age-session", "мне 43 года, хочу кроссовки")
        self.assertNotIn("size", _sessions["age-session"]["filters"])


class QuantityIsNotSizeTests(unittest.TestCase):
    """Количество и единица измерения не становятся размером (дефект D1 четвёртого аудита)."""

    def test_quantities_are_not_sizes(self):
        for message in ("43 пары", "43 штуки", "43 кг", "43 км", "43 см", "43 минуты", "хочу 43 пары"):
            with self.subTest(message=message):
                self.assertNotIn("size", parse_request(message))

    def test_quantities_do_not_confirm_a_budget_either(self):
        from app.services.chat import _grounded_ai_filters

        self.assertEqual(_grounded_ai_filters({"size": 43}, {"size": "43"}, "хочу 43 пары"), {})
        self.assertEqual(_grounded_ai_filters({"max_price": 43}, {"max_price": "43"}, "хочу 43 пары"), {})

    def test_currency_is_a_budget_not_a_quantity(self):
        self.assertEqual(parse_request("120 долларов"), {"max_price": 120.0, "currency": "USD"})
        self.assertEqual(parse_request("до 120 грн"), {"max_price": 120.0, "currency": "UAH"})

    def test_real_size_next_to_a_quantity_is_kept(self):
        self.assertEqual(parse_request("хочу 43 пары, размер 43"), {"size": 43.0})


class AgeAndSizeTogetherTests(unittest.TestCase):
    """Возраст и явно названный размер в одном сообщении (дефект D2 четвёртого аудита)."""

    def test_named_size_survives_the_age_rule(self):
        for message in ("мне 43 года, размер 43", "Размер 43, мне 43 года", "Мне 43 года и нужен 43 размер",
                        "Мені 43 років, розмір 43", "Мой возраст 43, нужен размер 43"):
            with self.subTest(message=message):
                self.assertEqual(parse_request(message).get("size"), 43.0)

    def test_model_size_is_confirmed_when_named_as_a_size(self):
        from app.services.chat import _grounded_ai_filters

        self.assertEqual(_grounded_ai_filters({"size": 43}, {"size": "43"}, "мне 43 года, размер 43"), {"size": 43})
        self.assertEqual(_grounded_ai_filters({"size": 43}, {"size": "43"}, "мне 43 года"), {})

    def test_age_alone_still_gives_no_size(self):
        for message in ("мне 43 года", "мені 43 роки", "43 роки"):
            with self.subTest(message=message):
                self.assertNotIn("size", parse_request(message))

    def test_age_does_not_block_a_different_size(self):
        self.assertEqual(parse_request("мне 43 года, размер 42"), {"size": 42.0})


class StrictAliasTests(unittest.TestCase):
    """Короткая словарная основа не срабатывает внутри постороннего слова (дефект D3).

    Длинные основы намеренно остаются префиксными: «прогул», «бездорож», «школь» обязаны
    находить свои формы.
    """

    def test_foreign_words_are_not_filters(self):
        for word, axis in (("бегемот", "use_case"), ("бегство", "use_case"), ("беглый", "use_case"),
                           ("залив", "use_case"), ("залп", "use_case"), ("найкер", "brand")):
            with self.subTest(word=word):
                self.assertNotIn(axis, parse_request(word))

    def test_strict_alias_forms_still_work(self):
        for word, expected in (("бег", {"use_case": "Running"}), ("бега", {"use_case": "Running"}),
                               ("беговые", {"use_case": "Running"}), ("зал", {"use_case": "Training"}),
                               ("зале", {"use_case": "Training"}), ("найки", {"brand": "Nike"}),
                               ("женские", {"gender": "Women"}), ("мужские", {"gender": "Men"})):
            with self.subTest(word=word):
                parsed = parse_request(word)
                for key, value in expected.items():
                    self.assertEqual(parsed.get(key), value)

    def test_long_stems_still_match_their_forms(self):
        for word, expected in (("бездорожью", "Trail"), ("для прогулок", "Walking"), ("школьные", "school")):
            with self.subTest(word=word):
                self.assertIn(expected, parse_request(word).values())

    def test_word_forms_are_explicit_and_never_generated(self):
        """Словоформы только объявлены явно: генерации «основа + окончание» быть не должно."""
        import app.catalog_rules as rules

        for gone in ("CYRILLIC_FORM_ENDINGS", "_build_alias_forms", "ALIAS_FORMS", "_axis_aliases", "_is_latin",
                     "WORD_ENDINGS", "STRICT_ALIASES", "_STRICT_GROUP", "_ENDING_GROUP"):
            self.assertFalse(hasattr(rules, gone), f"генератор форм остался: {gone}")
        # Объявленные формы присутствуют в словарях буквально.
        self.assertIn("красную", rules.COLOR_ALIASES["red"])
        self.assertIn("для трейла", rules.USE_CASE_ALIASES["Trail"])
        self.assertIn("найками", rules.BRAND_ALIASES)

    def test_soft_path_is_not_used_for_free_text(self):
        """Мягкая канонизация значения модели не участвует в разборе пользовательской фразы."""
        import app.catalog_rules as rules

        # Строгий путь: часть слова значением не становится.
        self.assertIsNone(rules.match_alias("белка", {"бел": "White"}))
        self.assertIsNone(rules.match_alias("redmi", {"red": "Red"}))
        # Мягкий путь существует отдельно и служит только отдельному значению модели.
        self.assertEqual(rules.match_value("бел", {"бел": "White"}), "White")
        self.assertEqual(rules.match_value("running", {"running": "Running"}), "Running")
        # В разборе текста мягкие основы не применяются.
        for word in ("бел", "черн", "сер", "син"):
            self.assertIsNone(rules.implied_value("color", f" {word} "))


class AuditRoundFiveTests(unittest.TestCase):
    """Регрессии на дефекты пятого аудита (валюты, единицы счёта, дефисный возраст, порядок «43 размер»)."""

    def test_other_currencies_are_budgets_not_sizes(self):
        for message in ("43 евро", "43 євро", "43 фунта", "43 злотых", "43 бака"):
            with self.subTest(message=message):
                parsed = parse_request(message)
                self.assertNotIn("size", parsed)
                self.assertEqual(parsed.get("max_price"), 43.0)

    def test_more_units_are_not_sizes(self):
        for message in ("43 комплекта", "43 коробки", "43 недели", "43 месяца", "43 мл воды",
                        "43 л", "43 м", "43 г", "43 тижні"):
            with self.subTest(message=message):
                self.assertNotIn("size", parse_request(message))

    def test_model_cannot_confirm_a_size_from_a_unit(self):
        from app.services.chat import _grounded_ai_filters

        self.assertEqual(_grounded_ai_filters({"size": 43}, {"size": "43"}, "43 комплекта"), {})
        self.assertEqual(_grounded_ai_filters({"size": 43}, {"size": "43"}, "43 евро"), {})

    def test_hyphenated_age_is_not_a_size(self):
        self.assertNotIn("size", parse_request("43-летие"))

    def test_size_named_after_the_number_survives_an_age(self):
        """«мне 43, нужен 43 размер»: размер назван словом после числа."""
        self.assertEqual(parse_request("мне 43, нужен 43 размер").get("size"), 43.0)
        from app.services.chat import _grounded_ai_filters

        self.assertEqual(_grounded_ai_filters({"size": 43}, {"size": "43"}, "мне 43, нужен 43 размер"), {"size": 43})

    def test_size_word_after_the_number_is_a_size(self):
        self.assertEqual(parse_request("43 размер").get("size"), 43.0)


class ExplicitLexicalFormsTests(unittest.TestCase):
    """Закрытый словарь форм: ложных совпадений нет, нужные формы объявлены (тесты A–N)."""

    def test_negative_examples_create_no_filter(self):
        for text, axis in (("нужны парные кроссовки", "gender"), ("залью фото позже", "use_case"),
                           ("коричный цвет", "color"), ("redmi note", "color"), ("trailer", "use_case"),
                           ("работаю в парнике", "gender"), ("прогулял физуру", "use_case"),
                           ("не могу дождаться новой коллекции", "feature"), ("бегемот", "use_case"),
                           ("залив", "use_case"), ("найкер", "brand")):
            with self.subTest(text=text):
                self.assertNotIn(axis, parse_request(text))

    def test_required_positive_forms(self):
        for text, expected in (("для трейла", {"use_case": "Trail"}), ("красную", {"color": "Red"}),
                               ("белую", {"color": "White"}), ("серую", {"color": "Grey"}),
                               ("синюю", {"color": "Blue"}), ("біла пара", {"color": "White"}),
                               ("девушке нужны кроссовки", {"gender": "Women"}),
                               ("шкільні кросівки", {"feature": "school"}),
                               ("осенние кроссовки", {"feature": "autumn"}), ("найками", {"brand": "Nike"}),
                               ("для пробежек", {"use_case": "Running"}),
                               ("для пробіжок", {"use_case": "Running"}),
                               ("по бездорожью", {"use_case": "Trail"}),
                               ("для прогулок", {"use_case": "Walking"}), ("в зале", {"use_case": "Training"}),
                               ("для бега", {"use_case": "Running"}), ("червоні", {"color": "Red"}),
                               ("жіночі", {"gender": "Women"})):
            with self.subTest(text=text):
                parsed = parse_request(text)
                for key, value in expected.items():
                    self.assertEqual(parsed.get(key), value)

    def test_required_brand_and_colour_forms(self):
        for text, expected in (("найк", "Nike"), ("найки", "Nike"), ("найками", "Nike"),
                               ("адидас", "Adidas"), ("адидасы", "Adidas"), ("пума", "Puma"), ("пумы", "Puma"),
                               ("асикс", "Asics")):
            with self.subTest(text=text):
                self.assertEqual(parse_request(text).get("brand"), expected)
        for text, expected in (("красный", "Red"), ("красная", "Red"), ("красные", "Red"), ("красную", "Red"),
                               ("червоний", "Red"), ("червона", "Red"), ("червоні", "Red"),
                               ("белый", "White"), ("белая", "White"), ("белые", "White"), ("белую", "White"),
                               ("білий", "White"), ("біла", "White"), ("білі", "White"),
                               ("серый", "Grey"), ("серая", "Grey"), ("серые", "Grey"), ("серую", "Grey"),
                               ("сірий", "Grey"), ("сіра", "Grey"), ("сірі", "Grey"),
                               ("синий", "Blue"), ("синяя", "Blue"), ("синие", "Blue"), ("синюю", "Blue"),
                               ("синій", "Blue"), ("синя", "Blue"), ("сині", "Blue")):
            with self.subTest(text=text):
                self.assertEqual(parse_request(text).get("color"), expected)

    def test_gender_phrases(self):
        for text, expected in (("мужские", "Men"), ("мужские кроссовки", "Men"), ("для мужчины", "Men"),
                               ("для парня", "Men"), ("женские", "Women"), ("для женщины", "Women"),
                               ("для девушки", "Women"), ("девушке нужны кроссовки", "Women")):
            with self.subTest(text=text):
                self.assertEqual(parse_request(text).get("gender"), expected)


class UkrainianRemovalTests(unittest.TestCase):
    """D3: украинские команды снятия условий (тесты O–P)."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_removal_phrases_are_recognised(self):
        for text, expected in (("бренд не важливий", {"brand"}), ("бренд не має значення", {"brand"}),
                               ("будь-який бренд", {"brand"}), ("колір не важливий", {"color"}),
                               ("будь-який колір", {"color"}), ("розмір не враховуй", {"size"}),
                               ("розмір не важливий", {"size"}), ("без обмеження ціни", {"price"}),
                               ("ціна не важлива", {"price"})):
            with self.subTest(text=text):
                self.assertEqual(detect_removals(text), expected)

    def test_o_brand_removal_keeps_the_size(self):
        _sessions.pop("uk-brand", None)
        chat(self.db, "uk-brand", "Nike 43")
        chat(self.db, "uk-brand", "бренд не важливий")
        state = dict(_sessions["uk-brand"]["filters"])
        self.assertNotIn("brand", state)
        self.assertEqual(state.get("size"), 43.0)

    def test_p_show_all_resets_filters(self):
        _sessions.pop("uk-all", None)
        chat(self.db, "uk-all", "червоні 42")
        self.assertEqual(dict(_sessions["uk-all"]["filters"]), {"color": "Red", "size": 42.0})
        chat(self.db, "uk-all", "покажи всі")
        self.assertEqual(dict(_sessions["uk-all"]["filters"]), {})
        self.assertTrue(_sessions["uk-all"]["browse"])

    def test_ukrainian_dialog_keeps_conditions(self):
        _sessions.pop("uk-dialog", None)
        chat(self.db, "uk-dialog", "чорні 43")
        result = chat(self.db, "uk-dialog", "для залу")
        self.assertEqual(dict(_sessions["uk-dialog"]["filters"]), {"color": "Black", "size": 43.0,
                                                                   "use_case": "Training"})
        self.assertIn("products", result)

    def test_similar_does_not_mutate_user_state(self):
        """D6: regression check — «похожі» не меняют условий пользователя."""
        _sessions.pop("uk-similar", None)
        for message in ("Nike 43", "не білі", "до 150", "для бігу"):
            chat(self.db, "uk-similar", message)
        before = dict(_sessions["uk-similar"]["filters"])
        chat(self.db, "uk-similar", "покажи схожі")
        self.assertEqual(dict(_sessions["uk-similar"]["filters"]), before)


class AuditRoundSevenTests(unittest.TestCase):
    """Регрессии на дефекты седьмого аудита (D1–D5)."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_hyphen_is_a_token_boundary(self):
        for text in ("до-школьный возраст", "pre-school class", "nike-like design", "не-пума"):
            with self.subTest(text=text):
                self.assertEqual(parse_request(text), {})

    def test_absent_properties_are_matched_strictly(self):
        for text in ("полетный режим", "взлетная полоса", "43-летний мужчина", "жовтень на дворі", "очень легко"):
            with self.subTest(text=text):
                self.assertEqual(detect_absent_properties(text), [])

    def test_absent_properties_still_recognise_real_wishes(self):
        self.assertEqual(detect_absent_properties("на лето"), ["summer"])

    def test_short_unit_does_not_become_a_size(self):
        self.assertNotIn("size", parse_request("43 шт"))
        self.assertNotIn("size", parse_request("нужно 43 шт"))

    def test_named_axis_is_not_removed_by_a_removal_phrase(self):
        """D2: явно названное значение оси не снимается фразой снятия другой оси."""
        self.assertIn("size", detect_removals("любой бренд, но размер 43"))
        self.assertIn("size", detect_removals("без ограничения по цене, размер 43"))
        self.assertIn("size", detect_removals("будь-який бренд, але розмір 43"))

    def test_d1_budget_survives_brand_removal(self):
        _sessions.pop("r7-budget", None)
        chat(self.db, "r7-budget", "Nike 43")
        result = chat(self.db, "r7-budget", "любой бренд по цене до 100")
        state = dict(_sessions["r7-budget"]["filters"])
        self.assertNotIn("brand", state)
        self.assertEqual(state.get("max_price"), 100.0)
        self.assertTrue(all(product["price"] <= 100 for product in result["products"]))

    def test_d2_explicit_size_survives_price_removal(self):
        _sessions.pop("r7-size", None)
        chat(self.db, "r7-size", "Nike 43")
        chat(self.db, "r7-size", "без ограничения по цене, размер 43")
        state = dict(_sessions["r7-size"]["filters"])
        self.assertNotIn("max_price", state)
        self.assertEqual(state.get("size"), 43.0)

    def test_missing_real_forms_are_declared(self):
        for text, expected in (("хочу бегать по утрам 42", {"use_case": "Running"}),
                               ("в спортзал", {"use_case": "Training"}),
                               ("для спортзала", {"use_case": "Training"}),
                               ("коричневе", {"color": "Brown"}), ("чорною", {"color": "Black"}),
                               ("сірою", {"color": "Grey"}), ("для мужика", {"gender": "Men"})):
            with self.subTest(text=text):
                parsed = parse_request(text)
                for key, value in expected.items():
                    self.assertEqual(parsed.get(key), value)


class NegationPersistenceTests(unittest.TestCase):
    """Отрицательное условие — такое же условие: оно не теряется на следующих ходах."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def session(self, name):
        _sessions.pop(name, None)
        return name

    def test_negation_is_stored_in_the_session(self):
        sid = self.session("neg-store")
        chat(self.db, sid, "Размер 43, только не Nike")
        self.assertEqual(_sessions[sid]["filters"]["exclude_brand"], ["Nike"])
        self.assertNotIn("brand", _sessions[sid]["filters"])

    def test_negation_survives_the_next_turn(self):
        sid = self.session("neg-next")
        chat(self.db, sid, "Размер 43, не Nike")
        result = chat(self.db, sid, "Покажи ещё")
        self.assertTrue(all(product["brand"] != "Nike" for product in result["products"]))

    def test_negation_survives_a_fallback_search(self):
        """Даже когда по условиям ничего не нашлось, исключение не выбрасывается."""
        sid = self.session("neg-fallback")
        chat(self.db, sid, "Размер 43, не Nike")
        result = chat(self.db, sid, "Кремовые, не Nike")
        self.assertNotIn("Кремовый", [product["color"] for product in result["products"]])
        self.assertTrue(all(product["brand"] != "Nike" for product in result["products"]))

    def test_negation_survives_the_broadest_search(self):
        """Широкое ослабление снимает бренд, цвет и цену, но не исключение пользователя."""
        sid = self.session("neg-broad")
        chat(self.db, sid, "Размер 43, не Nike")
        result = chat(self.db, sid, "Кремовые кроссовки 43")
        self.assertTrue(all(product["brand"] != "Nike" for product in result["products"]))

    def test_negation_survives_similar_request(self):
        sid = self.session("neg-similar")
        chat(self.db, sid, "Размер 43, не Nike")
        result = chat(self.db, sid, "Покажи похожие")
        self.assertTrue(all(product["brand"] != "Nike" for product in result["products"]))

    def test_colour_negation_survives_the_next_turn(self):
        sid = self.session("neg-color")
        chat(self.db, sid, "Размер 43, не белые")
        result = chat(self.db, sid, "Покажи ещё")
        self.assertTrue(all(product["color"] != "White" for product in result["products"]))

    def test_removal_also_clears_the_exclusion(self):
        """«цвет не важен» отменяет и исключение цвета: условие снято целиком."""
        sid = self.session("neg-remove")
        chat(self.db, sid, "Размер 43, не белые")
        chat(self.db, sid, "Цвет не важен")
        self.assertNotIn("exclude_attributes", _sessions[sid]["filters"])

    def test_new_search_clears_exclusions(self):
        sid = self.session("neg-new")
        chat(self.db, sid, "Размер 43, не Nike")
        chat(self.db, sid, "Начать заново")
        self.assertNotIn("exclude_brand", _sessions[sid]["filters"])

    def test_show_all_clears_exclusions(self):
        sid = self.session("neg-all")
        chat(self.db, sid, "Размер 43, не Nike")
        chat(self.db, sid, "Покажи все")
        self.assertNotIn("exclude_brand", _sessions[sid]["filters"])

    def test_exclusion_is_named_in_the_summary_and_in_the_reasons(self):
        sid = self.session("neg-text")
        result = chat(self.db, sid, "Размер 43, только не Nike")
        self.assertIn("не Nike", result["reply"])
        self.assertIn("не Nike", result["products"][0]["match_reasons"])

    def test_conflicting_condition_is_reported_and_exclusion_wins(self):
        sid = self.session("neg-conflict")
        result = chat(self.db, sid, "Nike, но не Nike, размер 43")
        self.assertIn("одновременно исключили", result["reply"])
        self.assertTrue(all(product["brand"] != "Nike" for product in result["products"]))


if __name__ == "__main__":
    unittest.main()
