import unittest
from unittest.mock import patch

from sqlalchemy import select

from app.ai.providers import build_prompt
from app.ai.tools import check_stock, get_product_details, search_products
from app.catalog_rules import BRAND_VALUES, COLOR_VALUES, FEATURE_VALUES, GENDER_VALUES, SNEAKER_CONFIG, USE_CASE_VALUES, lexicon
from app.database import SessionLocal, ensure_catalog_schema
from app.models import Attribute, ProductAttribute
from app.schemas import AIFilters, PRICE_MAX, sanitize_price
from app.seed import seed_database
from app.services.assistant import parse_request
from app.services.chat import _normalize_ai_filters, _sessions, chat
from app.services.chat_sessions import load_session_state


class ChatToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_brand_change_clears_previous_model_ru_uk(self):
        for language in ("ru", "uk"):
            with self.subTest(language=language):
                session_id = "brand-change-" + language
                _sessions.pop(session_id, None)
                try:
                    chat(self.db, session_id, "Adidas Superstar", language)
                    result = chat(self.db, session_id, "Nike 43 до $120", language)
                    self.assertTrue(result["products"])
                    self.assertNotIn("name_query", _sessions[session_id]["filters"])
                    self.assertTrue(all(
                        product["brand"] == "Nike" and product["price"] <= 120
                        and 43 in product["available_sizes"]
                        for product in result["products"]
                    ))
                finally:
                    _sessions.pop(session_id, None)

    def test_search_products(self):
        products = search_products(self.db, brand="Nike", color="White", size=43, max_price=120)
        self.assertTrue(any(product["name"] == "Air Max Pulse" for product in products))

    def test_currency_budget_parser_keeps_amount_and_currency_separate(self):
        for phrase, currency, price in (
            ("кроссовки до €120", "EUR", 120),
            ("кроссовки до 3000 грн", "UAH", 3000),
            ("Nike до $150", "USD", 150),
        ):
            with self.subTest(phrase=phrase):
                filters = parse_request(phrase)
                self.assertEqual(filters["currency"], currency)
                self.assertEqual(filters["max_price"], price)

    def test_chat_filters_a_hryvnia_budget_and_returns_prices_in_display_currency(self):
        session_id = "display-currency-uah-budget"
        _sessions.pop(session_id, None)
        rates = {"available": True, "rates": {"EUR": 1.0, "USD": 1.1, "UAH": 44.0}}
        try:
            with patch("app.services.products.get_rates", return_value=rates), \
                    patch("app.services.chat.get_rates", return_value=rates):
                result = chat(self.db, session_id, "Nike до 5000 гривен размер 43", "ru", "UAH")
            self.assertTrue(result["products"])
            self.assertEqual(result["filters"]["currency"], "UAH")
            self.assertTrue(all(item["currency"] == "UAH" for item in result["products"]))
            self.assertTrue(all(item["price"] <= 5000 for item in result["products"]))
            self.assertTrue(all(item["base_currency"] == "USD" for item in result["products"]))
        finally:
            _sessions.pop(session_id, None)

    def test_purpose_filter_uses_catalog_data(self):
        products = search_products(self.db, use_case="Running", size=43)
        self.assertTrue(products)
        self.assertTrue(all(product["use_case"] == "Running" for product in products))

    def test_small_size_search_uses_catalog_data(self):
        products = search_products(self.db, size=36)
        self.assertTrue(products)
        self.assertTrue(all(36.0 in product["available_sizes"] for product in products))

    def test_check_stock_and_sold_out_size(self):
        self.assertTrue(check_stock(self.db, 1, 43))
        self.assertFalse(check_stock(self.db, 1, 44))

    def test_unknown_product(self):
        self.assertIsNone(get_product_details(self.db, 999999))

    def test_context_and_fallback_without_provider(self):
        _sessions.pop("test-context", None)
        first = chat(self.db, "test-context", "Нужны Nike до $120")
        second = chat(self.db, "test-context", "43")
        self.assertTrue(first["needs_clarification"])
        self.assertEqual(first["mode"], "fallback")
        self.assertEqual(first["suggestions"], ["36", "38", "42", "43"])
        self.assertFalse(second["needs_clarification"])
        self.assertTrue(any(product["brand"] == "Nike" for product in second["products"]))

    def test_natural_budget_phrases_have_deterministic_support(self):
        self.assertEqual(parse_request("Мне нужны кроссы примерно за сотку"), {"min_price": 80.0, "max_price": 120.0})
        self.assertEqual(parse_request("Есть что-то adidas недорогое?"), {"brand": "Adidas", "max_price": 120.0})

    def test_ukrainian_parsing_and_reply(self):
        self.assertEqual(parse_request("Білі Nike 43 до $120"), {"brand": "Nike", "color": "White", "currency": "USD", "size": 43.0, "max_price": 120.0})
        self.assertEqual(parse_request("кросівки адідас"), {"brand": "Adidas"})
        _sessions.pop("test-uk", None)
        result = chat(self.db, "test-uk", "Білі Nike 43 до $120", "uk")
        self.assertTrue(result["products"])
        self.assertTrue(result["reply"].startswith("Знайшов"))
        self.assertIn("Покажи дешевше", result["suggestions"])

    def test_ukrainian_known_model_shows_product_instead_of_school_question(self):
        _sessions.pop("test-uk-superstar", None)
        result = chat(self.db, "test-uk-superstar", "Добрий вечір. А у вас є кросівки адідас суперстар?", "uk")

        self.assertFalse(result["needs_clarification"])
        self.assertNotIn("для прогулянок, бігу, залу", result["reply"])
        self.assertTrue(result["products"])
        self.assertTrue(any(product["brand"] == "Adidas" and product["name"] == "Superstar" for product in result["products"]))

    def test_unknown_ai_values_do_not_become_database_filters(self):
        self.assertEqual(_normalize_ai_filters({"brand": "Nike", "color": "фиолетовые", "gender": "unknown"}), {"brand": "Nike"})

    def test_follow_up_keeps_context_and_can_find_cheaper_products(self):
        _sessions.pop("test-follow-up", None)
        first = chat(self.db, "test-follow-up", "Adidas Ultraboost Light 43")
        second = chat(self.db, "test-follow-up", "Покажи подешевле")
        self.assertTrue(first["products"])
        self.assertTrue(second["products"])
        self.assertLess(max(product["price"] for product in second["products"]), min(product["price"] for product in first["products"]))

    def test_chat_context_survives_backend_state_cache_reset(self):
        session_id = "persist-context-test"
        _sessions.pop(session_id, None)
        first = chat(self.db, session_id, "Nike 43")
        expected_filters = dict(_sessions[session_id]["filters"])
        _sessions.pop(session_id, None)  # имитируем пустой кэш после перезапуска backend
        restored = load_session_state(self.db, session_id)
        self.assertEqual(restored["filters"], expected_filters)
        self.assertEqual(restored["shown_product_ids"], [item["id"] for item in first["products"]])

    def test_more_restores_filters_and_excludes_already_shown_products(self):
        session_id = "persist-more-test"
        _sessions.pop(session_id, None)
        first = chat(self.db, session_id, "Nike 43")
        _sessions.pop(session_id, None)
        second = chat(self.db, session_id, "Покажи ещё")
        self.assertFalse(second["needs_clarification"])
        self.assertEqual(_sessions[session_id]["filters"]["brand"], "Nike")
        self.assertEqual(_sessions[session_id]["filters"]["size"], 43.0)
        self.assertFalse({item["id"] for item in first["products"]} & {item["id"] for item in second["products"]})

    def test_ai_reply_must_match_a_backend_grounded_candidate(self):
        import json
        from app.ai.providers import OllamaProvider, ProviderUnavailable

        candidates = ["Нашёл варианты из каталога.", "Подобрал модели по вашему запросу."]
        approved = json.dumps({"response": json.dumps({"reply": candidates[1]}, ensure_ascii=False)})
        with patch.object(OllamaProvider, "_post", return_value=approved):
            self.assertEqual(OllamaProvider("http://127.0.0.1:11434", "stub").respond(candidates), candidates[1])
        with patch.object(OllamaProvider, "_post", return_value='{"response":"{\\"reply\\":\\"Nike Air Max за $1, размер 99 есть в наличии\\"}"}'):
            with self.assertRaises(ProviderUnavailable):
                OllamaProvider("http://127.0.0.1:11434", "stub").respond(candidates)

    def test_vague_request_gets_one_useful_clarifying_question(self):
        _sessions.pop("test-vague", None)
        vague = chat(self.db, "test-vague", "Хочу удобные кроссовки")
        goal = chat(self.db, "test-vague", "Для прогулок")
        self.assertTrue(vague["needs_clarification"])
        self.assertIn("прогулок", vague["reply"])
        self.assertEqual(vague["suggestions"], ["Для прогулок", "Для бега", "Для зала", "Для школы"])
        self.assertTrue(goal["needs_clarification"])
        self.assertIn("размер", goal["reply"])

    def test_short_greeting_gets_friendly_start_message(self):
        _sessions.pop("test-greeting", None)
        result = chat(self.db, "test-greeting", "ку")
        self.assertIn("Привет", result["reply"])
        self.assertEqual(result["products"], [])
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(result["suggestions"], [])

    def test_trekking_request_returns_real_catalog_products(self):
        _sessions.pop("test-trail", None)
        result = chat(self.db, "test-trail", "Кроссовки для трекинга по пересечённой местности 43")
        self.assertTrue(result["products"])
        self.assertTrue(all(product["use_case"] == "Trail" for product in result["products"]))

    def test_trekking_question_confirms_catalogue_before_asking_size(self):
        _sessions.pop("test-trail-question", None)
        result = chat(self.db, "test-trail-question", "Кроссовки для трекинга у вас есть?")
        self.assertIn("есть модели для трекинга", result["reply"])
        self.assertTrue(result["needs_clarification"])

    def test_unavailable_hard_size_does_not_show_other_sizes_as_matches(self):
        _sessions.pop("test-nearby-size", None)
        result = chat(self.db, "test-nearby-size", "Белые Nike 44 до $120")
        self.assertFalse(result["products"])
        self.assertEqual(result["filters"]["size"], 44.0)
        self.assertIn("44", result["reply"])

    def test_no_match_in_hard_budget_does_not_offer_over_budget_models(self):
        _sessions.pop("test-near-budget", None)
        result = chat(self.db, "test-near-budget", "Nike 44 до $100")
        self.assertFalse(result["products"])
        self.assertEqual(result["filters"]["max_price"], 100.0)
        self.assertEqual(result["filters"]["size"], 44.0)

    def test_follow_up_can_change_color_without_losing_other_filters(self):
        _sessions.pop("test-other-color", None)
        first = chat(self.db, "test-other-color", "Белые Nike 43")
        second = chat(self.db, "test-other-color", "Другого цвета")
        self.assertTrue(first["products"])
        self.assertTrue(second["products"])
        self.assertTrue(any(product["color"] != "White" for product in second["products"]))
        self.assertEqual(_sessions["test-other-color"]["filters"]["brand"], "Nike")
        self.assertEqual(_sessions["test-other-color"]["filters"]["size"], 43.0)

    def test_show_more_returns_new_products_without_repeating_first_page(self):
        _sessions.pop("test-more", None)
        first = chat(self.db, "test-more", "43")
        second = chat(self.db, "test-more", "Покажи ещё")
        first_ids = {product["id"] for product in first["products"]}
        second_ids = {product["id"] for product in second["products"]}
        self.assertTrue(second_ids)
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(second["reply"], "Показываю ещё подходящие варианты.")

    def test_running_goal_filters_real_catalog_products(self):
        _sessions.pop("test-goal", None)
        first = chat(self.db, "test-goal", "Для бега")
        second = chat(self.db, "test-goal", "43")
        self.assertTrue(first["needs_clarification"])
        self.assertTrue(second["products"])
        self.assertTrue(all(product["use_case"] == "Running" for product in second["products"]))

    def test_purpose_parser_understands_russian_and_ukrainian(self):
        self.assertEqual(parse_request("Для бега, 43"), {"use_case": "Running", "size": 43.0})
        self.assertEqual(parse_request("Для залу 43"), {"use_case": "Training", "size": 43.0})

    def test_lifestyle_feature_parser_understands_russian_and_ukrainian(self):
        self.assertEqual(parse_request("Для школы, размер 42"), {"feature": "school", "size": 42.0})
        self.assertEqual(parse_request("На осінь, розмір 43"), {"feature": "autumn", "size": 43.0})
        self.assertEqual(parse_request("Щоб не промокали, 43"), {"feature": "water_resistant", "size": 43.0})

    def test_feature_request_returns_only_database_tagged_products(self):
        _sessions.pop("test-water-resistant", None)
        result = chat(self.db, "test-water-resistant", "Не промокают, размер 43")
        self.assertTrue(result["products"])
        self.assertTrue(all("water_resistant" in product["features"] for product in result["products"]))

    def test_changing_purpose_removes_an_unstated_previous_feature(self):
        _sessions.pop("test-change-purpose", None)
        chat(self.db, "test-change-purpose", "Не промокают, размер 43")
        result = chat(self.db, "test-change-purpose", "Для зала, размер 43")
        self.assertTrue(result["products"])
        self.assertTrue(all(product["use_case"] == "Training" for product in result["products"]))
        self.assertNotIn("feature", _sessions["test-change-purpose"]["filters"])

    def test_no_more_results_does_not_repeat_product_cards(self):
        _sessions.pop("test-no-more", None)
        chat(self.db, "test-no-more", "Air Max 43")
        result = chat(self.db, "test-no-more", "Покажи ещё")
        self.assertFalse(result["products"])
        self.assertIn("больше моделей нет", result["reply"])

    def test_valid_fake_provider_analysis(self):
        class FakeProvider:
            def analyze(self, history, message):
                return {
                    "intent": "search_products",
                    "filters": {"brand": "Nike", "size": 43, "use_case": "Running"},
                    "evidence": {"use_case": "пробежк"},
                }
        _sessions.pop("fake-valid", None)
        with patch("app.services.chat.get_provider", return_value=FakeProvider()):
            result = chat(self.db, "fake-valid", "Nike на пробежку, 43")
        self.assertEqual(_sessions["fake-valid"]["filters"]["use_case"], "Running")
        self.assertEqual(result["mode"], "ollama")
        self.assertTrue(any(product["use_case"] == "Running" for product in result["products"]))

    def test_provider_filter_with_evidence_is_applied(self):
        class EvidenceProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"color": "Black"}, "evidence": {"color": "тёмные"}}
        _sessions.pop("fake-evidence", None)
        with patch("app.services.chat.get_provider", return_value=EvidenceProvider()):
            result = chat(self.db, "fake-evidence", "Хочу тёмные кроссовки, размер 43")
        self.assertEqual(_sessions["fake-evidence"]["filters"]["color"], "Black")
        self.assertEqual(result["mode"], "ollama")
        self.assertTrue(result["products"])
        self.assertTrue(all(product["color"] == "Black" for product in result["products"]))

    def test_provider_filter_without_evidence_is_ignored(self):
        class SilentProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"color": "Black"}}
        _sessions.pop("fake-no-evidence", None)
        with patch("app.services.chat.get_provider", return_value=SilentProvider()):
            result = chat(self.db, "fake-no-evidence", "Хочу тёмные кроссовки, размер 43")
        self.assertNotIn("color", _sessions["fake-no-evidence"]["filters"])
        self.assertEqual(result["mode"], "fallback")

    def test_provider_evidence_must_come_from_the_message(self):
        class LyingProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"color": "Red"}, "evidence": {"color": "красные"}}
        _sessions.pop("fake-lying", None)
        with patch("app.services.chat.get_provider", return_value=LyingProvider()):
            result = chat(self.db, "fake-lying", "Хочу тёмные кроссовки, размер 43")
        self.assertNotIn("color", _sessions["fake-lying"]["filters"])
        self.assertEqual(result["mode"], "fallback")

    def test_evidence_comparison_ignores_case_and_punctuation(self):
        class CaseProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"color": "Black"}, "evidence": {"color": "Тёмные"}}
        _sessions.pop("fake-case", None)
        with patch("app.services.chat.get_provider", return_value=CaseProvider()):
            result = chat(self.db, "fake-case", "Хочу тёмные кроссовки, размер 43")
        self.assertEqual(result["mode"], "ollama")
        self.assertEqual(_sessions["fake-case"]["filters"]["color"], "Black")

    def test_invalid_provider_uses_fallback(self):
        class BadProvider:
            def analyze(self, history, message):
                return {"wrong": "shape"}
        _sessions.pop("fake-invalid", None)
        with patch("app.services.chat.get_provider", return_value=BadProvider()):
            result = chat(self.db, "fake-invalid", "Nike 43")
        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["mode"], "fallback")

    def test_provider_cannot_add_unstated_gender(self):
        class HallucinatingProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"brand": "Nike", "size": 44, "gender": "Women"}}
        _sessions.pop("fake-gender", None)
        with patch("app.services.chat.get_provider", return_value=HallucinatingProvider()):
            result = chat(self.db, "fake-gender", "Nike 44")
        self.assertTrue(any(product["name"] == "Pegasus 41" for product in result["products"]))

    def test_gender_parser_understands_common_male_wording(self):
        self.assertEqual(parse_request("Нужны кроссовки для парня"), {"gender": "Men"})

    def test_price_between_30_and_50_is_not_detected_as_size(self):
        self.assertEqual(parse_request("Покажи кроссовки до $40"), {"currency": "USD", "max_price": 40.0})
        self.assertEqual(parse_request("Есть что-то недорогое до 45?"), {"max_price": 45.0})
        self.assertEqual(parse_request("Недорого, до 35"), {"max_price": 35.0})
        self.assertEqual(parse_request("Цена 40"), {"max_price": 40.0})
        self.assertEqual(parse_request("Хочу за 40"), {"max_price": 40.0})
        self.assertEqual(parse_request("Ищу за 40-45 долларов"), {"currency": "USD", "min_price": 40.0, "max_price": 45.0})
        self.assertNotIn("size", parse_request("Покажи что-нибудь до 50"))

    def test_price_words_of_any_kind_do_not_become_size(self):
        for message, expected in (
            ("с бюджетом 35", 35.0), ("бюджет 40", 40.0), ("максимум 45", 45.0), ("лимит 40", 40.0),
            ("не больше 40", 40.0), ("в пределах 40", 40.0), ("по цене 40", 40.0),
            ("потратить 45", 45.0), ("рублей 40", 40.0), ("у.е. 40", 40.0),
        ):
            with self.subTest(message=message):
                self.assertEqual(parse_request(message), {"max_price": expected})
        self.assertNotIn("size", parse_request("дешевле 45"))

    def test_size_word_next_to_a_price_word_is_a_size(self):
        self.assertEqual(parse_request("до 45 размера"), {"size": 45.0})
        self.assertEqual(parse_request("максимум 43 размер"), {"size": 43.0})
        self.assertEqual(parse_request("размер до 45"), {"size": 45.0})
        self.assertEqual(parse_request("в размере до 45"), {"size": 45.0})

    def test_negated_price_ceiling_sets_only_the_maximum(self):
        self.assertEqual(parse_request("не дороже 45"), {"max_price": 45.0})
        self.assertEqual(parse_request("дороже 100"), {"min_price": 100.0})

    def test_approximate_budget_is_not_a_size(self):
        self.assertEqual(parse_request("примерно 40"), {"min_price": 20.0, "max_price": 60.0})
        self.assertEqual(parse_request("около 45"), {"min_price": 25.0, "max_price": 65.0})
        self.assertEqual(parse_request("десь 35"), {"min_price": 15.0, "max_price": 55.0})

    def test_size_and_budget_are_kept_together(self):
        self.assertEqual(parse_request("Nike 43 размера до 150"), {"brand": "Nike", "size": 43.0, "max_price": 150.0})
        self.assertEqual(parse_request("43 розміру до 150"), {"size": 43.0, "max_price": 150.0})

    def test_price_word_inside_another_word_is_not_a_price(self):
        self.assertEqual(parse_request("надо 43"), {"size": 43.0})
        self.assertEqual(parse_request("этот 43"), {"size": 43.0})

    def test_currency_amount_sets_a_budget(self):
        self.assertEqual(parse_request("40 рублей"), {"max_price": 40.0})
        self.assertEqual(parse_request("43 грн"), {"currency": "UAH", "max_price": 43.0})
        self.assertEqual(parse_request("ліміт 40"), {"max_price": 40.0})
        self.assertEqual(parse_request("40 гривен"), {"currency": "UAH", "max_price": 40.0})
        self.assertEqual(parse_request("в межах 40"), {"max_price": 40.0})
        self.assertEqual(parse_request("40 р"), {"max_price": 40.0})
        self.assertEqual(parse_request("размер 43 р"), {"size": 43.0})

    def test_cheap_words_with_a_number_are_a_budget(self):
        self.assertEqual(parse_request("недорого, 40"), {"max_price": 40.0})
        self.assertEqual(parse_request("недорого 43"), {"max_price": 43.0})
        self.assertEqual(parse_request("недорого"), {"max_price": 120.0})

    def test_budget_stated_before_a_size_is_kept(self):
        self.assertEqual(parse_request("до 150 размер 43"), {"max_price": 150.0, "size": 43.0})
        self.assertEqual(parse_request("Nike до 120 размер 43"), {"brand": "Nike", "max_price": 120.0, "size": 43.0})
        self.assertEqual(parse_request("до 45 размер 43"), {"max_price": 45.0, "size": 43.0})
        self.assertEqual(parse_request("цена 45 размер 43"), {"max_price": 45.0, "size": 43.0})
        self.assertEqual(parse_request("от 45 размер 43"), {"min_price": 45.0, "size": 43.0})
        self.assertEqual(parse_request("до 45, размер 43"), {"max_price": 45.0, "size": 43.0})

    def test_cheap_word_with_a_bare_number_prefers_the_budget(self):
        # Осознанное решение: голое число после «недорого» считается бюджетом, размер не подставляется.
        self.assertEqual(parse_request("недорого, 43"), {"max_price": 43.0})
        self.assertEqual(parse_request("недорого, 45 размер"), {"max_price": 120.0, "size": 45.0})

    def test_approximate_size_request_gets_no_budget(self):
        self.assertEqual(parse_request("примерно 43 размера"), {"size": 43.0})

    def test_inverted_price_range_is_retained_for_clarification(self):
        self.assertEqual(parse_request("от 100 до 50"), {"min_price": 100.0, "max_price": 50.0})

    def test_negation_typo_and_discounted_amounts(self):
        self.assertEqual(parse_request("недороже 45"), {"max_price": 45.0})
        self.assertEqual(parse_request("скидка 45"), {})

    def test_size_is_still_detected_without_a_price(self):
        self.assertEqual(parse_request("43"), {"size": 43.0})
        self.assertEqual(parse_request("Нужны кроссовки 38 размера"), {"size": 38.0})
        self.assertEqual(parse_request("розмір 44"), {"size": 44.0})
        self.assertEqual(parse_request("Белые Nike 43 до $120"), {"brand": "Nike", "color": "White", "currency": "USD", "size": 43.0, "max_price": 120.0})

    def test_size_is_not_taken_from_a_longer_number(self):
        self.assertNotIn("size", parse_request("Есть 430?"))
        self.assertNotIn("size", parse_request("Модель 2024 года"))

    def test_ai_price_fields_accept_only_sane_numbers(self):
        self.assertEqual(AIFilters(max_price=120).max_price, 120.0)
        self.assertEqual(AIFilters(min_price=100, max_price=200).min_price, 100.0)
        self.assertIsNone(AIFilters(min_price=-1).min_price)
        self.assertIsNone(AIFilters(max_price=PRICE_MAX + 1).max_price)
        self.assertIsNone(AIFilters(max_price="120").max_price)
        self.assertIsNone(AIFilters(min_price=True).min_price)
        self.assertIsNone(sanitize_price(float("inf")))
        self.assertIsNone(sanitize_price(float("nan")))

    def test_ai_price_range_must_be_consistent(self):
        inverted = AIFilters(min_price=200, max_price=100)
        self.assertIsNone(inverted.min_price)
        self.assertIsNone(inverted.max_price)

    def test_normalized_ai_filters_drop_invalid_prices(self):
        self.assertEqual(_normalize_ai_filters({"min_price": -1, "max_price": 150}), {"max_price": 150.0})
        self.assertEqual(_normalize_ai_filters({"min_price": 300, "max_price": 100}), {})
        self.assertEqual(_normalize_ai_filters({"brand": "Nike", "max_price": PRICE_MAX + 1}), {"brand": "Nike"})
        self.assertEqual(_normalize_ai_filters({"max_price": "120"}), {})

    def test_model_price_without_evidence_is_not_applied_at_all(self):
        class PriceOnFirstMessageProvider:
            def __init__(self):
                self.calls = 0

            def analyze(self, history, message):
                self.calls += 1
                filters = {"max_price": 40} if self.calls == 1 else {}
                return {"intent": "search_products", "filters": filters}

        _sessions.pop("fake-price-stick", None)
        with patch("app.services.chat.get_provider", return_value=PriceOnFirstMessageProvider()):
            first = chat(self.db, "fake-price-stick", "Хочу кроссовки, размер 43")
            second = chat(self.db, "fake-price-stick", "Покажи ещё")
        self.assertEqual(first["mode"], "fallback")
        self.assertNotIn("max_price", _sessions["fake-price-stick"]["filters"])
        self.assertEqual(_sessions["fake-price-stick"]["filters"]["size"], 43.0)
        self.assertTrue(second["products"])

    def test_model_price_borrowing_a_size_number_applies_only_to_that_turn(self):
        class BorrowingProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"max_price": 60}, "evidence": {"max_price": "43"}}

        _sessions.pop("fake-price-borrow", None)
        with patch("app.services.chat.get_provider", return_value=BorrowingProvider()):
            chat(self.db, "fake-price-borrow", "Хочу кроссовки, размер 43")
            second = chat(self.db, "fake-price-borrow", "43")
        self.assertNotIn("max_price", _sessions["fake-price-borrow"]["filters"])
        self.assertTrue(second["products"])

    def test_model_cannot_point_at_a_function_word(self):
        class SingleLetterProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"color": "Black"}, "evidence": {"color": "и"}}

        _sessions.pop("fake-letter", None)
        with patch("app.services.chat.get_provider", return_value=SingleLetterProvider()):
            result = chat(self.db, "fake-letter", "Нужны кроссовки Nike 43")
        self.assertNotIn("color", _sessions["fake-letter"]["filters"])
        self.assertEqual(result["mode"], "fallback")

    def test_model_cannot_borrow_another_attribute_as_evidence(self):
        class BorrowingProvider:
            def __init__(self, filters):
                self.filters = filters

            def analyze(self, history, message):
                return {"intent": "search_products", "filters": self.filters, "evidence": {key: "белые" for key in self.filters}}

        for filters in ({"brand": "Asics"}, {"gender": "Women"}, {"use_case": "Trail"}, {"feature": "school"}):
            with self.subTest(filters=filters):
                session = f"fake-borrow-{next(iter(filters))}"
                _sessions.pop(session, None)
                with patch("app.services.chat.get_provider", return_value=BorrowingProvider(filters)):
                    result = chat(self.db, session, "Нужны белые кроссовки 43")
                for key in filters:
                    self.assertNotIn(key, _sessions[session]["filters"])
                self.assertEqual(result["mode"], "fallback")

    def test_model_evidence_cannot_contradict_the_value(self):
        class ContradictingProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"color": "Black"}, "evidence": {"color": "бел"}}

        _sessions.pop("fake-contradict", None)
        with patch("app.services.chat.get_provider", return_value=ContradictingProvider()):
            result = chat(self.db, "fake-contradict", "Нужны белые кроссовки 43")
        self.assertEqual(_sessions["fake-contradict"]["filters"]["color"], "White")
        self.assertEqual(result["mode"], "fallback")

    def test_short_colour_stems_do_not_match_inside_other_words(self):
        self.assertEqual(parse_request("Кроссовки на пробежку"), {})
        self.assertEqual(parse_request("Серебристые кроссовки 43"), {"color": "Silver", "size": 43.0})
        self.assertEqual(parse_request("Чёрные кроссовки 43"), {"color": "Black", "size": 43.0})

    def test_model_size_confirmed_by_a_borrowed_number_does_not_persist(self):
        class BorrowingProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"size": 44}, "evidence": {"size": "3"}}

        _sessions.pop("fake-size-borrow", None)
        with patch("app.services.chat.get_provider", return_value=BorrowingProvider()):
            first = chat(self.db, "fake-size-borrow", "Хочу Nike, 3 пары")
            second = chat(self.db, "fake-size-borrow", "Покажи ещё")
        # Уточнённый контракт: чужое число больше не подтверждает размер даже на один ход,
        # поэтому вклад модели отклоняется сразу и выдача остаётся детерминированной.
        self.assertEqual(first["mode"], "fallback")
        self.assertNotIn("size", _sessions["fake-size-borrow"]["filters"])
        self.assertEqual(second["mode"], "fallback")

    def test_user_size_is_not_replaced_by_a_model_value(self):
        class OtherSizeProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"size": 41}, "evidence": {"size": "43"}}

        _sessions.pop("fake-size-keep", None)
        with patch("app.services.chat.get_provider", return_value=OtherSizeProvider()):
            chat(self.db, "fake-size-keep", "Хочу Nike 43")
            chat(self.db, "fake-size-keep", "Покажи ещё")
        self.assertEqual(_sessions["fake-size-keep"]["filters"]["size"], 43.0)

    def test_model_numeric_filter_needs_a_number_in_the_evidence(self):
        class NoNumberProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"size": 43}, "evidence": {"size": "кроссовки"}}

        _sessions.pop("fake-no-number", None)
        with patch("app.services.chat.get_provider", return_value=NoNumberProvider()):
            result = chat(self.db, "fake-no-number", "Нужны белые кроссовки")
        self.assertNotIn("size", _sessions["fake-no-number"]["filters"])
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(result["mode"], "fallback")

    def test_user_stated_budget_survives_follow_up(self):
        _sessions.pop("price-context", None)
        chat(self.db, "price-context", "Nike 43 до $160")
        chat(self.db, "price-context", "Покажи ещё")
        self.assertEqual(_sessions["price-context"]["filters"]["max_price"], 160.0)
        self.assertEqual(_sessions["price-context"]["filters"]["size"], 43.0)

    def test_ai_price_cannot_override_user_stated_budget(self):
        class CheapProvider:
            def analyze(self, history, message):
                return {"intent": "search_products", "filters": {"max_price": 50}}

        _sessions.pop("fake-price-override", None)
        with patch("app.services.chat.get_provider", return_value=CheapProvider()):
            chat(self.db, "fake-price-override", "Nike 43 до $160")
            second = chat(self.db, "fake-price-override", "Nike 43")
        self.assertEqual(_sessions["fake-price-override"]["filters"]["max_price"], 160.0)
        self.assertTrue(second["products"])
        self.assertTrue(all(product["price"] <= 160 for product in second["products"]))

    def test_prompt_is_built_from_the_catalog_rule_source(self):
        prompt = build_prompt([], "тестовое сообщение")
        for value in (*BRAND_VALUES.values(), *COLOR_VALUES.values(), *GENDER_VALUES, *USE_CASE_VALUES, *FEATURE_VALUES):
            with self.subTest(value=value):
                self.assertIn(value, prompt)
        self.assertIn("evidence", prompt)

    def test_lexicon_lists_the_same_colors_as_the_parser(self):
        catalog = lexicon()
        for color in COLOR_VALUES.values():
            self.assertIn(color, catalog)
        self.assertIn("коричн", catalog)

    def test_every_color_in_the_catalog_can_be_recognized(self):
        # Цвет теперь атрибут, а не колонка: читаем значения из атрибутов каталога.
        catalog_colors = {
            value.lower()
            for (value,) in self.db.execute(
                select(ProductAttribute.value)
                .join(ProductAttribute.attribute)
                .where(Attribute.code == "color")
                .distinct()
            )
        }
        self.assertTrue(catalog_colors)
        self.assertLessEqual(catalog_colors, {value.lower() for value in COLOR_VALUES.values()})

    def test_trail_colors_are_recognized_from_the_chat(self):
        self.assertEqual(parse_request("Коричневые кроссовки"), {"color": "Brown"})
        self.assertEqual(parse_request("Оранжевые"), {"color": "Orange"})
        self.assertEqual(parse_request("Оливковые, 43"), {"color": "Olive", "size": 43.0})

    def test_brand_is_recognized_in_cyrillic(self):
        self.assertEqual(parse_request("Хочу найк 43"), {"brand": "Nike", "size": 43.0})
        self.assertEqual(parse_request("адидас"), {"brand": "Adidas"})

    def test_ai_values_are_canonicalized_through_the_shared_dictionary(self):
        self.assertEqual(_normalize_ai_filters({"color": "gray"}), {"color": "Grey"})
        self.assertEqual(_normalize_ai_filters({"color": "бел"}), {"color": "White"})
        self.assertEqual(_normalize_ai_filters({"gender": "women"}), {"gender": "Women"})
        self.assertEqual(_normalize_ai_filters({"brand": "найк"}), {"brand": "Nike"})


if __name__ == "__main__":
    unittest.main()
