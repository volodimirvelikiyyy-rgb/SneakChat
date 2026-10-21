"""Контракт доменного gate через настоящий HTTP /api/chat.

Проверяется главный инвариант: если сообщение не является товарным запросом, то ни состояние
диалога, ни условия поиска, ни режим просмотра/похожих измениться не могут — даже если
внутренний парсер увидел в тексте кандидат вроде числа 43 или слова «белая». Внутренний парсер
может вернуть кандидата: важно, что pipeline его не применяет.
"""

import unittest

from fastapi.testclient import TestClient

from app.catalog_rules import shopping_intent
from app.database import SessionLocal, ensure_catalog_schema
from app.main import app
from app.seed import seed_database
from app.services.assistant import parse_request
from app.services.chat import _sessions

# Сообщения, которые не являются запросом о товаре, но содержат слова-кандидаты.
NON_SHOPPING_WITH_CANDIDATES = (
    "Купити квиток на 43 місце",
    "Купити квиток за 300",
    "Купити квиток на 43 місце за 300",
    "модель телефона белая",
    "покажи все новости за неделю",
    "покажи все новости",
    "любой размеренный темп",
    "покажи любой кадр со сцены",
    "пума на дереві",
    "літня людина",
    "я работаю в офисе",
    "пойду в зал завтра",
    "бегемот в зоопарке",
    "trailer приехал",
    "redmi note",
    "залью фото позже",
)


class ShoppingGateHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def send(self, session_id, message):
        response = self.client.post("/api/chat", json={"session_id": session_id, "message": message, "language": "ru"})
        self.assertEqual(response.status_code, 200, f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def fresh(self, name):
        _sessions.pop(name, None)
        return name

    def state(self, session_id):
        return dict(_sessions[session_id]["filters"])

    def snapshot(self, session_id):
        session = _sessions[session_id]
        return (dict(session["filters"]), bool(session.get("browse")), session.get("similar_overlay"),
                list(session.get("shown_product_ids") or []))

    # --- D1/D2 девятого аудита через HTTP ------------------------------------

    def test_d1_ticket_messages_create_nothing(self):
        for index, message in enumerate(("Купити квиток на 43 місце", "Купити квиток за 300",
                                         "Купити квиток на 43 місце за 300")):
            session = self.fresh(f"gate-d1-{index}")
            payload = self.send(session, message)
            with self.subTest(message=message):
                self.assertEqual(self.state(session), {})
                self.assertFalse(payload["products"])
                self.assertNotIn("Какой размер", payload["reply"])
                self.assertNotIn("размер вам нужен", payload["reply"])

    def test_d1_phone_message_creates_nothing(self):
        session = self.fresh("gate-phone")
        payload = self.send(session, "модель телефона белая")
        self.assertEqual(self.state(session), {})
        self.assertFalse(payload["products"])
        self.assertNotIn("White", str(self.state(session)))

    def test_d2_news_message_does_not_reset_in_active_session(self):
        session = self.fresh("gate-news")
        first = self.send(session, "Nike 43")
        before = self.snapshot(session)
        payload = self.send(session, "покажи все новости за неделю")
        self.assertEqual(self.state(session), {"brand": "Nike", "size": 43.0})
        self.assertFalse(_sessions[session]["browse"], "browse-mode не должен включаться")
        self.assertEqual(self.snapshot(session), before)
        # Контракт Этапа 5: нетоварная фраза не запускает поиск и не возвращает выдачу,
        # даже если условия в сессии остались.
        self.assertFalse(payload["products"])
        self.assertEqual(_sessions[session]["shown_product_ids"],
                         [item["id"] for item in first["products"]])

    def test_d2_tempo_message_does_not_remove_size(self):
        session = self.fresh("gate-tempo")
        self.send(session, "Nike 43")
        self.send(session, "любой размеренный темп")
        self.assertEqual(self.state(session), {"brand": "Nike", "size": 43.0})

    def test_d2_frame_message_does_not_remove_budget(self):
        session = self.fresh("gate-frame")
        self.send(session, "Nike 43 до 120")
        self.send(session, "покажи любой кадр со сцены")
        self.assertEqual(self.state(session), {"brand": "Nike", "size": 43.0, "max_price": 120.0, "currency": "UAH"})

    # --- активный сеанс не превращает любую фразу в follow-up -----------------

    def test_active_session_is_protected_from_foreign_phrases(self):
        for index, message in enumerate(("модель телефона белая", "покажи все новости за неделю",
                                         "пума на дереві", "Купити квиток за 300")):
            session = self.fresh(f"gate-active-{index}")
            self.send(session, "Nike 43 до 120")
            before = self.snapshot(session)
            with self.subTest(message=message):
                self.send(session, message)
                self.assertEqual(self.state(session), {"brand": "Nike", "size": 43.0, "max_price": 120.0, "currency": "UAH"})
                self.assertEqual(self.snapshot(session), before)

    def test_saved_search_result_is_not_overwritten_by_foreign_phrase(self):
        """Нетоварная фраза не перезаписывает сохранённую выдачу (дефект двенадцатого аудита).

        Проверка идёт на состоянии, которое отличается от базового поиска: после «похожих»
        и после «покажи ещё» сохранённая выдача своя, поэтому её подмена видна.
        """
        for index, (setup, foreign) in enumerate(((["Nike 43", "покажи похожие"], "сброс пароля не работает"),
                                                  (["Nike 43", "покажи ещё"], "вкус похож на малину"),
                                                  (["Nike 43", "покажи ещё"], "покажи ещё новости"))):
            session = self.fresh(f"gate-products-{index}")
            for message in setup:
                self.send(session, message)
            before = self.snapshot(session)
            payload = self.send(session, foreign)
            with self.subTest(setup=setup, foreign=foreign):
                self.assertEqual(_sessions[session]["products"], _sessions[session]["products"])
                self.assertEqual(self.snapshot(session), before)
                self.assertFalse(payload["products"], "выдача не должна возвращаться")

    def test_greeting_and_more_do_not_change_modes(self):
        """Нетоварные реплики не выключают browse и не продолжают выдачу."""
        session = self.fresh("gate-modes")
        self.send(session, "покажи все")
        before = self.snapshot(session)
        for message in ("привет", "здравствуйте", "покажи ещё новости", "покажи ещё фотографии"):
            with self.subTest(message=message):
                self.send(session, message)
                self.assertEqual(self.snapshot(session), before)

    def test_brand_is_not_replaced_by_an_animal_name(self):
        session = self.fresh("gate-puma")
        self.send(session, "Nike 43")
        self.send(session, "пума на дереві")
        self.assertEqual(self.state(session)["brand"], "Nike")

    # --- короткие настоящие follow-up работают --------------------------------

    def test_short_follow_ups_still_work(self):
        session = self.fresh("gate-follow")
        self.send(session, "ищу кроссовки")
        steps = (("Puma", {"brand": "Puma"}), ("43", {"size": 43.0}), ("красные", {"color": "Red"}),
                 ("до 100", {"max_price": 100.0}))
        for message, expected in steps:
            self.send(session, message)
            state = self.state(session)
            with self.subTest(message=message):
                for key, value in expected.items():
                    self.assertEqual(state.get(key), value)
        self.send(session, "не белые")
        self.assertEqual(self.state(session)["exclude_attributes"], {"color": ["White"]})

    # --- архитектурный инвариант ---------------------------------------------

    def test_gate_blocks_candidates_that_the_parser_can_see(self):
        """Парсер может вернуть кандидата — pipeline его не применяет.

        Это главный инвариант: кандидат из нетоварной фразы не меняет ни состояние, ни условия
        поиска, ни режимы просмотра и похожих, и не запускает поиск товара.
        """
        candidates = 0
        for index, message in enumerate(NON_SHOPPING_WITH_CANDIDATES):
            session = self.fresh(f"gate-inv-{index}")
            parser_saw = parse_request(message)
            self.assertFalse(shopping_intent(message, parser_saw), f"сообщение должно быть нетоварным: {message}")
            if parser_saw:
                candidates += 1
            before = self.snapshot(session)
            payload = self.send(session, message)
            with self.subTest(message=message):
                self.assertEqual(self.state(session), {}, "состояние не должно пополняться")
                self.assertEqual(self.snapshot(session), before, "режимы и показанные товары не меняются")
                self.assertFalse(payload["products"], "поиск товара не запускается")
                self.assertNotIn("Какой размер", payload["reply"])
        self.assertGreater(candidates, 0, "проверка бессмысленна, если парсер не видит ни одного кандидата")

    def test_parser_can_see_a_candidate_in_a_non_shopping_phrase(self):
        """Кандидаты действительно существуют — именно поэтому gate и нужен."""
        self.assertEqual(parse_request("Купити квиток на 43 місце"), {"size": 43.0})
        self.assertEqual(parse_request("модель телефона белая"), {"color": "White"})
        self.assertEqual(parse_request("покажи все новости за неделю"), {})


class SimilarOverlayRegressionTests(unittest.TestCase):
    """D6: «похожие» не переписывают условия пользователя."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_similar_keeps_user_filters(self):
        _sessions.pop("gate-similar", None)
        for message in ("Nike", "43", "не белые", "до 150", "для бега", "для зала"):
            self.client.post("/api/chat", json={"session_id": "gate-similar", "message": message, "language": "ru"})
        before = dict(_sessions["gate-similar"]["filters"])
        self.client.post("/api/chat", json={"session_id": "gate-similar", "message": "покажи похожие", "language": "ru"})
        self.assertEqual(dict(_sessions["gate-similar"]["filters"]), before)
        self.assertEqual(before, {"brand": "Nike", "size": 43.0, "max_price": 150.0, "currency": "UAH", "use_case": "Training",
                                  "exclude_attributes": {"color": ["White"]}})


if __name__ == "__main__":
    unittest.main()
