"""Acceptance corpus SneakChat: зафиксированный набор реальных запросов и нетоварных фраз.

Ожидаемые значения написаны вручную и не генерируются кодом, который проверяется. Корпус
задаёт продуктовый контракт: SneakChat хорошо понимает реальные запросы о выборе кроссовок
на русском и украинском через закрытый доменный словарь. Морфологического анализа нет, поэтому
редкая форма может не распознаться — это безопасный false negative, если фильтр просто не
применяется и ложный факт не создаётся.
"""

import json
import unittest
from pathlib import Path

from app.ai.grounding import reply_amounts, verify_reply
from app.catalog_rules import shopping_intent
from app.database import SessionLocal, ensure_catalog_schema
from app.seed import seed_database
from app.services.assistant import parse_request
from app.services.chat import _sessions, chat

# --- одиночные запросы: сообщение → ожидаемые условия ------------------------
_CORPUS_PATH = Path(__file__).parent / "data" / "ai_seller_cases.jsonl"
_CORPUS = [json.loads(line) for line in _CORPUS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
ACCEPTANCE = [(case["input"], case["expected_filters"]) for case in _CORPUS if case["execution"] == "parser"]

# --- отрицания: сообщение → ожидаемые исключения в состоянии сессии ----------
NEGATIONS: list[tuple[str, dict]] = [
    ("только не Nike", {"exclude_brand": ["Nike"]}),
    ("не белые", {"exclude_attributes": {"color": ["White"]}}),
    ("не для бега", {"exclude_attributes": {"purpose": ["Running"]}}),
    ("белые не нужны", {"exclude_attributes": {"color": ["White"]}}),
    ("Nike не нужен", {"exclude_brand": ["Nike"]}),
    ("не 43", {"exclude_attributes": {"size": [43.0]}}),
    ("43 не нужен", {"exclude_attributes": {"size": [43.0]}}),
]

# --- снятие условий -----------------------------------------------------------
REMOVALS: list[tuple[str, dict]] = [
    ("бренд не важен", {"brand"}),
    ("любой бренд", {"brand"}),
    ("цвет не важен", {"color"}),
    ("любой цвет", {"color"}),
    ("размер не учитывай", {"size"}),
    ("без ограничения по цене", {"price"}),
    ("цена не важна", {"price"}),
    ("покажи все", {"all", "brand", "color", "size", "price"}),
    ("бренд не важливий", {"brand"}),
    ("будь-який бренд", {"brand"}),
    ("колір не важливий", {"color"}),
    ("розмір не враховуй", {"size"}),
    ("без обмеження ціни", {"price"}),
    ("ціна не важлива", {"price"}),
    ("покажи всі", {"all", "brand", "color", "size", "price"}),
]

# --- обычный текст, не связанный с покупкой обуви -----------------------------
NON_SHOPPING = [case["input"] for case in _CORPUS if case["execution"] == "gate"]

# --- multi-turn сценарии: реплики → условия пользователя в состоянии ---------
MULTI_TURN: list[tuple[list[str], dict]] = [
    (["ищу кроссовки", "Puma", "43", "красные"], {"brand": "Puma", "size": 43.0, "color": "Red"}),
    (["Nike 43", "до 150"], {"brand": "Nike", "size": 43.0, "max_price": 150.0, "currency": "USD"}),
    (["для бега 42", "не Nike"], {"use_case": "Running", "size": 42.0, "exclude_brand": ["Nike"]}),
    (["Nike", "43", "не белые", "до 150", "для бега", "для зала"],
     {"brand": "Nike", "size": 43.0, "max_price": 150.0, "currency": "USD", "use_case": "Training",
      "exclude_attributes": {"color": ["White"]}}),
]


class AcceptanceCorpusTests(unittest.TestCase):
    """Все реальные запросы корпуса разбираются в ожидаемые условия."""

    def test_acceptance_corpus(self):
        for message, expected in ACCEPTANCE:
            with self.subTest(message=message):
                self.assertEqual(parse_request(message), expected)

    def test_acceptance_corpus_is_intent(self):
        for message, _expected in ACCEPTANCE:
            with self.subTest(message=message):
                self.assertTrue(shopping_intent(message, parse_request(message)))

    def test_negation_corpus(self):
        from app.catalog_rules import detect_negations

        for message, expected in NEGATIONS:
            with self.subTest(message=message):
                recognised = detect_negations(message)
                for key, values in expected.items():
                    if key == "exclude_brand":
                        self.assertEqual(recognised.get("brand"), values)
                        continue
                    # Код атрибута каталога приводится к имени оси диалога.
                    code, items = next(iter(values.items()))
                    axis = {"purpose": "use_case", "tags": "feature"}.get(code, code)
                    self.assertEqual(recognised.get(axis), items)

    def test_removal_corpus(self):
        from app.catalog_rules import detect_removals

        for message, expected in REMOVALS:
            with self.subTest(message=message):
                self.assertEqual(detect_removals(message), expected)


class NonShoppingCorpusTests(unittest.TestCase):
    """Обычный текст без товарного контекста не создаёт фильтров каталога."""

    def test_no_filters_without_context(self):
        for message in NON_SHOPPING:
            with self.subTest(message=message):
                self.assertFalse(shopping_intent(message, parse_request(message)))

    def test_dialog_keeps_state_empty(self):
        ensure_catalog_schema()
        db = SessionLocal()
        try:
            seed_database(db)
            for index, message in enumerate(NON_SHOPPING):
                session = f"nonshop-{index}"
                _sessions.pop(session, None)
                chat(db, session, message)
                with self.subTest(message=message):
                    self.assertEqual(dict(_sessions[session]["filters"]), {})
        finally:
            db.close()


class ShoppingContextFollowUpTests(unittest.TestCase):
    """Активный товарный контекст разрешает короткие follow-up реплики."""

    @classmethod
    def setUpClass(cls):
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_short_follow_ups_work_in_context(self):
        _sessions.pop("follow", None)
        chat(self.db, "follow", "ищу кроссовки")
        steps = [("Puma", {"brand": "Puma"}), ("43", {"size": 43.0}), ("красные", {"color": "Red"}),
                 ("до 100", {"max_price": 100.0}), ("для бега", {"use_case": "Running"})]
        for message, expected in steps:
            chat(self.db, "follow", message)
            state = dict(_sessions["follow"]["filters"])
            with self.subTest(message=message):
                for key, value in expected.items():
                    self.assertEqual(state.get(key), value)

    def test_short_follow_ups_of_negation_and_similar(self):
        _sessions.pop("follow-neg", None)
        chat(self.db, "follow-neg", "ищу кроссовки")
        chat(self.db, "follow-neg", "43")
        chat(self.db, "follow-neg", "не белые")
        self.assertEqual(_sessions["follow-neg"]["filters"]["exclude_attributes"], {"color": ["White"]})
        before = dict(_sessions["follow-neg"]["filters"])
        chat(self.db, "follow-neg", "покажи похожие")
        self.assertEqual(dict(_sessions["follow-neg"]["filters"]), before)

    def test_multi_turn_scenarios(self):
        for index, (messages, expected) in enumerate(MULTI_TURN):
            session = f"multi-{index}"
            _sessions.pop(session, None)
            for message in messages:
                chat(self.db, session, message)
            state = dict(_sessions[session]["filters"])
            with self.subTest(messages=messages):
                self.assertEqual(state, expected)

    def test_show_more_after_similar_keeps_state(self):
        _sessions.pop("more", None)
        for message in ("Nike 43", "не белые", "до 150"):
            chat(self.db, "more", message)
        before = dict(_sessions["more"]["filters"])
        chat(self.db, "more", "покажи похожие")
        chat(self.db, "more", "покажи ещё")
        self.assertEqual(dict(_sessions["more"]["filters"]), before)

    def test_browse_and_reset(self):
        _sessions.pop("browse", None)
        chat(self.db, "browse", "Nike 43")
        chat(self.db, "browse", "покажи все")
        self.assertEqual(dict(_sessions["browse"]["filters"]), {})
        self.assertTrue(_sessions["browse"]["browse"])
        chat(self.db, "browse", "начать заново")
        self.assertEqual(dict(_sessions["browse"]["filters"]), {})
        self.assertFalse(_sessions["browse"]["browse"])


class UahGroundingTests(unittest.TestCase):
    """UAH проверяется наравне с грн и ₴."""

    def test_uah_amounts_are_read(self):
        for text, expected in (("999 UAH", {999.0}), ("999 uah", {999.0}), ("999 грн", {999.0}),
                               ("999 ₴", {999.0}), ("999 UAH.", {999.0})):
            with self.subTest(text=text):
                self.assertEqual(reply_amounts(text), expected)

    def test_unconfirmed_uah_amount_is_blocked(self):
        product = {"id": 1, "price": 110.0}
        for text in ("Сумма 999 UAH.", "Сумма 999 uah.", "Итого 999 ₴."):
            with self.subTest(text=text):
                self.assertTrue(verify_reply(text, [product], {}))

    def test_confirmed_uah_amount_passes(self):
        product = {"id": 1, "price": 110.0}
        self.assertEqual(verify_reply("Цена 110 UAH.", [product], {}), [])


if __name__ == "__main__":
    unittest.main()
