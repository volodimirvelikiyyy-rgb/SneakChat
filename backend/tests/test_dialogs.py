"""Диалоговые тесты SneakChat: сценарии A–M из задания и механики вокруг них.

Проверяется поведение диалога как продавца: помнить условия, менять и снимать их одной фразой,
задавать не больше одного вопроса, честно ослаблять условия и объяснять рекомендацию только
данными каталога.
"""

import unittest

from app.catalog_rules import detect_absent_properties, detect_removals
from app.database import SessionLocal, ensure_catalog_schema
from app.seed import seed_database
from app.services.chat import _sessions, chat


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def conversation(self, *messages, session=None, language="ru"):
        name = session or f"dialog-{messages[0][:20]}"
        _sessions.pop(name, None)
        results = [chat(self.db, name, message, language) for message in messages]
        return results, _sessions[name]["filters"]

    # A. «Нужны кроссовки для школы» → «42»: назначение и размер вместе.
    def test_a_school_request_then_size_keeps_both_conditions(self):
        results, filters = self.conversation("Нужны кроссовки для школы", "42")
        self.assertTrue(results[0]["needs_clarification"])
        self.assertEqual(filters["feature"], "school")
        self.assertEqual(filters["size"], 42.0)
        self.assertTrue(results[1]["products"])
        self.assertTrue(all("school" in product["features"] for product in results[1]["products"]))

    # B. «Хочу Nike 43» → «лучше чёрные»: бренд и размер сохраняются.
    def test_b_change_of_colour_keeps_brand_and_size(self):
        results, filters = self.conversation("Хочу Nike 43", "лучше чёрные")
        self.assertEqual(filters, {"brand": "Nike", "size": 43.0, "color": "Black"})
        self.assertTrue(all(product["color"] == "Black" for product in results[1]["products"]))

    # C. «Adidas для бега до 150» → «максимум 100»: бюджет заменяется, остальное остаётся.
    def test_c_new_budget_replaces_the_old_one(self):
        _, filters = self.conversation("Adidas для бега до 150", "максимум 100")
        self.assertEqual(filters["brand"], "Adidas")
        self.assertEqual(filters["use_case"], "Running")
        self.assertEqual(filters["max_price"], 100.0)

    # D. «Nike 42» → «бренд не важен»: размер остаётся, бренд уходит.
    def test_d_brand_removal_keeps_the_size(self):
        results, filters = self.conversation("Nike 42", "бренд не важен")
        self.assertEqual(filters, {"size": 42.0})
        self.assertTrue(results[1]["products"])
        self.assertTrue(any(product["brand"] != "Nike" for product in results[1]["products"]))

    # E. «голубые для мальчика в школу»: цвет, пол и тег вместе.
    def test_e_colour_gender_and_school_together(self):
        _, filters = self.conversation("голубые для мальчика в школу")
        self.assertEqual(filters, {"color": "Blue", "gender": "Men", "feature": "school"})

    # F. «чтобы не промокали на осень»: только существующие признаки каталога.
    def test_f_only_real_catalogue_tags_are_used(self):
        results, filters = self.conversation("чтобы не промокали на осень", "42")
        self.assertEqual(filters["feature"], ["autumn", "water_resistant"])
        products = results[1]["products"]
        self.assertTrue(products)
        for product in products:
            self.assertIn("water_resistant", product["features"])
            self.assertIn("autumn", product["features"])

    # G. Жёсткие ограничения остаются в силе, пока покупатель сам их не ослабил.
    def test_g_hard_constraints_are_not_relaxed_without_consent(self):
        results, filters = self.conversation("Nike 43 голубые до $80")
        result = results[0]
        self.assertFalse(result["products"])
        self.assertEqual(filters["brand"], "Nike")
        self.assertEqual(filters["color"], "Blue")
        self.assertEqual(filters["max_price"], 80.0)
        self.assertIn("можно ослабить", result["reply"].lower())
        self.assertNotIn("без ограничений", result["reply"])
        # Размер не ослаблен: показываем только то, что реально есть в наличии.
        self.assertTrue(all(43.0 in product["available_sizes"] for product in result["products"]))

    # H. Ответ на вопрос о размере трактуется как размер.
    def test_h_answer_to_size_question_is_a_size(self):
        _, filters = self.conversation("Хочу Nike", "43")
        self.assertEqual(filters, {"brand": "Nike", "size": 43.0})

    # I. «до 40 долларов» — это цена, а не размер.
    def test_i_budget_is_not_read_as_size(self):
        _, filters = self.conversation("до 40 долларов")
        self.assertNotIn("size", filters)
        self.assertEqual(filters["max_price"], 40.0)

    # J. Цвет заменяется, а не накапливается.
    def test_j_only_the_last_colour_survives(self):
        _, filters = self.conversation("чёрные", "белые", "лучше голубые")
        self.assertEqual(filters, {"color": "Blue"})

    # K. «покажи дешевле» сохраняет бренд и размер.
    def test_k_cheaper_keeps_brand_and_size_and_lowers_budget(self):
        results, filters = self.conversation("Adidas Ultraboost Light 43", "покажи дешевле")
        self.assertEqual(filters["brand"], "Adidas")
        self.assertEqual(filters["size"], 43.0)
        self.assertLess(filters["max_price"], min(product["price"] for product in results[0]["products"]))
        self.assertTrue(results[1]["products"])

    # L. «а Adidas?» меняет бренд и сохраняет назначение.
    def test_l_brand_switch_keeps_purpose(self):
        _, filters = self.conversation("Nike для бега", "а Adidas?")
        self.assertEqual(filters, {"brand": "Adidas", "use_case": "Running"})

    # M. «покажи похожие» ссылается на предыдущую выдачу.
    def test_m_similar_refers_to_the_previous_result(self):
        results, _ = self.conversation("Nike 43", "покажи похожие")
        first = results[0]["products"][0]
        similar = results[1]
        self.assertIn(first["name"], similar["reply"])
        first_ids = {product["id"] for product in results[0]["products"]}
        self.assertTrue(similar["products"])
        self.assertFalse(first_ids & {product["id"] for product in similar["products"]})


class ConditionEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def conversation(self, *messages, session, language="ru"):
        _sessions.pop(session, None)
        results = [chat(self.db, session, message, language) for message in messages]
        return results, _sessions[session]["filters"]

    def test_removal_phrases_are_recognized(self):
        expected = {
            "бренд не важен": "brand",
            "любой бренд": "brand",
            "цвет не важен": "color",
            "любой цвет": "color",
            "размер не учитывай": "size",
            "без ограничения по цене": "price",
            "цена не важна": "price",
            "не обязательно Nike": "brand",
            "інший бренд": "brand",
            "будь-який колір": "color",
        }
        for message, key in expected.items():
            with self.subTest(message=message):
                self.assertIn(key, detect_removals(message.lower()))

    def test_price_removal_clears_both_bounds(self):
        _, filters = self.conversation("Nike 43 от 100 до 150", "без ограничения по цене", session="dlg-price-remove")
        self.assertNotIn("max_price", filters)
        self.assertNotIn("min_price", filters)
        self.assertEqual(filters["size"], 43.0)

    def test_size_stays_ignored_on_the_next_turns(self):
        # Снятое условие не должно возвращаться вопросом на следующем ходу.
        results, filters = self.conversation(
            "Nike 43", "размер не учитывай", "покажи ещё", session="dlg-nosize-stays"
        )
        self.assertNotIn("size", filters)
        self.assertFalse(results[2]["needs_clarification"])
        self.assertTrue(results[2]["products"])
        self.assertNotIn("Какой размер", results[2]["reply"])

    def test_similar_keeps_the_requested_size(self):
        # «Похожие» не должны молча показать товар без нужного размера.
        results, filters = self.conversation("Nike 43", "покажи похожие", session="dlg-similar-size")
        self.assertEqual(filters.get("size"), 43.0)
        self.assertTrue(results[1]["products"])
        for product in results[1]["products"]:
            self.assertIn(43.0, product["available_sizes"])

    def test_broadest_fallback_names_only_real_conditions(self):
        # Жёсткий бюджет сохраняется: альтернативы вне него нельзя показывать без согласия.
        results, filters = self.conversation("Nike 43 голубые до $80", session="dlg-kept")
        self.assertFalse(results[0]["products"])
        self.assertEqual(filters["max_price"], 80.0)
        self.assertIn("бюджет", results[0]["reply"])
        self.assertNotIn("назначение", results[0]["reply"])

    def test_summary_shows_the_lower_bound(self):
        results, filters = self.conversation("Nike 43 от 200", session="dlg-min-price")
        self.assertEqual(filters["min_price"], 200.0)
        self.assertIn("от $200", results[0]["reply"])

    def test_search_without_size_does_not_crash(self):
        # Размер снят, основная и цветовая выдача пустые: движок не должен падать.
        results, filters = self.conversation(
            "Nike 43", "размер не учитывай", "Nike оливковые до $10", session="dlg-nosize-crash"
        )
        self.assertNotIn("size", filters)
        self.assertTrue(results[2]["reply"])
        for product in results[2]["products"]:
            self.assertTrue(product["available_sizes"])

    def test_similar_without_size_does_not_crash(self):
        results, _ = self.conversation("Nike 43", "покажи все", "Похожие", session="dlg-similar-nosize")
        self.assertTrue(results[2]["reply"])

    def test_browse_mode_continues_on_the_next_turns(self):
        results, filters = self.conversation("покажи все", "покажи ещё", session="dlg-browse")
        self.assertEqual(filters, {})
        self.assertTrue(results[1]["products"])
        self.assertNotIn("Подскажу точнее", results[1]["reply"])

    def test_new_search_leaves_browse_mode(self):
        results, filters = self.conversation("покажи все", "начать заново", session="dlg-browse-reset")
        self.assertEqual(filters, {})
        self.assertTrue(results[1]["needs_clarification"])
        self.assertIn("Подскажу точнее", results[1]["reply"])

    def test_greeting_answers_even_in_browse_mode(self):
        results, _ = self.conversation("покажи все", "привет", session="dlg-browse-greeting")
        self.assertIn("Привет", results[1]["reply"])
        self.assertEqual(results[1]["products"], [])

    def test_stating_a_condition_leaves_browse_mode(self):
        results, filters = self.conversation("Nike 43", "покажи все", "белые", session="dlg-browse-exit")
        self.assertEqual(filters, {"color": "White"})
        self.assertTrue(results[2]["products"])

    def test_show_all_clears_everything_and_shows_catalogue(self):
        results, filters = self.conversation("Nike 43", "покажи все", session="dlg-all")
        self.assertEqual(filters, {})
        self.assertTrue(results[1]["products"])
        self.assertIn("каталога", results[1]["reply"])

    def test_size_removal_stops_asking_about_size(self):
        results, filters = self.conversation("Nike 43", "размер не учитывай", session="dlg-nosize")
        self.assertNotIn("size", filters)
        self.assertFalse(results[1]["needs_clarification"])
        self.assertTrue(results[1]["products"])
        self.assertIn("размер не учитываю", results[1]["reply"])

    def test_inconsistent_budget_across_turns_is_retained_for_clarification(self):
        results, filters = self.conversation("Nike 43 от 150", "максимум 100", session="dlg-budget")
        self.assertEqual(filters["min_price"], 150.0)
        self.assertEqual(filters["max_price"], 100.0)
        self.assertEqual(results[1]["reply"].count("?"), 1)

    def test_new_search_resets_conditions(self):
        _, filters = self.conversation("Nike 43 для бега", "начать заново Adidas", session="dlg-reset")
        self.assertEqual(filters, {"brand": "Adidas"})


class LanguageAndQuestionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def conversation(self, *messages, session, language="ru"):
        _sessions.pop(session, None)
        results = [chat(self.db, session, message, language) for message in messages]
        return results, _sessions[session]["filters"]

    def test_required_live_language_requests(self):
        cases = {
            "для школы": {"feature": "school"},
            "мальчику в школу": {"gender": "Men", "feature": "school"},
            "девочке": {"gender": "Women"},
            "на осень": {"feature": "autumn"},
            "на дождь": {"feature": "water_resistant"},
            "чтобы не промокали": {"feature": "water_resistant"},
            "для бега": {"use_case": "Running"},
            "бегать по асфальту": {"use_case": "Running"},
            "для трекинга": {"use_case": "Trail"},
            "по бездорожью": {"use_case": "Trail"},
            "для прогулок": {"use_case": "Walking"},
            "ходить целый день": {"use_case": "Walking"},
            "на каждый день": {"use_case": "Lifestyle"},
            "розовые": {},
            "голубые": {"color": "Blue"},
            "белые": {"color": "White"},
            "чёрные": {"color": "Black"},
            "Nike 42": {"brand": "Nike", "size": 42.0},
            "Adidas 44 для бега": {"brand": "Adidas", "size": 44.0, "use_case": "Running"},
            "до $100": {"max_price": 100.0, "currency": "USD"},
            "не дороже 80": {"max_price": 80.0, "currency": "USD"},
            "примерно за 120": {},
            "что-нибудь недорогое": {"max_price": 120.0, "currency": "USD"},
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                _, filters = self.conversation(message, session=f"lang-{message}")
                for key, value in expected.items():
                    self.assertEqual(filters.get(key), value)
                if message == "примерно за 120":
                    self.assertEqual(_sessions[f"lang-{message}"]["preferences"]["price_target"],
                                     {"amount": 120.0, "currency": "USD"})
                self.assertLessEqual(set(filters) - {"name_query"}, set(expected) | {"size", "brand", "color", "use_case", "feature", "gender"})

    def test_absent_properties_are_answered_honestly(self):
        cases = {"розовые 43": "pink", "тёплые на зиму 43": "winter", "на лето 43": "summer"}
        for message, code in cases.items():
            with self.subTest(message=message):
                self.assertIn(code, detect_absent_properties(message.lower()))

    def test_absent_property_is_explained_and_not_invented(self):
        results, filters = self.conversation("розовые 43", session="dlg-absent")
        result = results[0]
        self.assertNotIn("color", filters)
        self.assertIn("нет розовых моделей", result["reply"])
        self.assertTrue(result["products"])
        self.assertTrue(all(product["color"] != "Pink" for product in result["products"]))

    def test_useful_request_is_not_turned_into_a_questionnaire(self):
        results, _ = self.conversation("Чёрные Nike 43 до $120", session="dlg-noquiz")
        result = results[0]
        self.assertFalse(result["needs_clarification"])
        for question in ("Какой бренд", "Какой цвет", "Какой бюджет", "Какое назначение"):
            self.assertNotIn(question, result["reply"])
        self.assertIn("цвет", result["reply"])

    def test_vague_request_gets_one_short_question(self):
        results, filters = self.conversation("что-нибудь удобное", session="dlg-vague")
        self.assertTrue(results[0]["needs_clarification"])
        self.assertEqual(filters, {})
        self.assertEqual(results[0]["suggestions"], ["Для прогулок", "Для бега", "Для зала", "Для школы"])

    def test_ukrainian_dialog_keeps_conditions(self):
        results, filters = self.conversation("Кросівки для школи", "42", session="dlg-uk", language="uk")
        self.assertEqual(filters["feature"], "school")
        self.assertEqual(filters["size"], 42.0)
        self.assertTrue(results[1]["reply"].startswith("Знайшов"))


class ResponseQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def conversation(self, *messages, session, language="ru"):
        _sessions.pop(session, None)
        results = [chat(self.db, session, message, language) for message in messages]
        return results, _sessions[session]["filters"]

    def test_every_product_explains_itself_from_catalogue_data(self):
        results, _ = self.conversation("Nike 43 для бега", session="dq-reasons")
        product = results[0]["products"][0]
        reasons = product["match_reasons"]
        self.assertTrue(reasons)
        self.assertIn("размер 43 в наличии", reasons)
        self.assertIn("для бега", reasons)
        self.assertIn("бренд Nike", reasons)

    def test_explanation_uses_only_known_phrases(self):
        results, _ = self.conversation("Nike 43 голубые до $120", session="dq-phrases")
        allowed = (
            "бренд", "цвет", "размер", "в наличии", "для", "на ", "укладывается в бюджет",
            "мужская модель", "женская модель", "унисекс", "с защитой", "модель из",
        )
        for product in results[0]["products"]:
            for reason in product["match_reasons"]:
                with self.subTest(reason=reason):
                    self.assertTrue(any(part in reason for part in allowed))

    def test_recommendation_reply_is_built_from_the_same_reasons(self):
        results, _ = self.conversation("Nike 43 для бега", "какие лучше?", session="dq-recommend")
        product = results[1]["products"][0]
        self.assertIn(product["name"], results[1]["reply"])
        self.assertIn("размер 43 в наличии", results[1]["reply"])

    def test_size_without_stock_is_never_offered(self):
        results, _ = self.conversation("Nike 42", session="dq-stock")
        self.assertTrue(results[0]["products"])
        for product in results[0]["products"]:
            self.assertIn(42.0, product["available_sizes"])
        self.assertNotIn("Dunk Low Retro", [product["name"] for product in results[0]["products"]])

    def test_size_question_offers_size_chips(self):
        results, _ = self.conversation("Для бега", session="dq-chips")
        self.assertEqual(results[0]["suggestions"], ["36", "38", "42", "43"])

    def test_follow_up_chips_are_understood_by_the_dialog(self):
        results, _ = self.conversation("Nike 43", session="dq-chip-flow")
        chips = results[0]["suggestions"]
        for chip in chips:
            with self.subTest(chip=chip):
                follow = chat(self.db, "dq-chip-flow", chip)
                self.assertTrue(follow["reply"])
                self.assertNotIn("Ничего", follow["reply"])


if __name__ == "__main__":
    unittest.main()
