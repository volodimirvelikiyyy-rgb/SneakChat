"""Контракт Ollama-провайдера: любое поведение модели ведёт к безопасному fallback.

Заглушка поднимается по-настоящему по HTTP, а провайдер конструируется продовой функцией
`get_provider()` из переменных окружения. Поэтому проверяется реальный путь: настройки,
транспорт, разбор конверта и разбора запроса — а не подмена внутренних функций.
"""

import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.ai.providers import OllamaProvider, ProviderUnavailable, get_provider
from app.ai.validation import REASON_UNEXPECTED_KEY, validate_analysis
from app.database import SessionLocal, ensure_catalog_schema
from app.main import app
from app.seed import seed_database
from ollama_stub import OllamaStub, unreachable_base_url

# Все аварийные режимы обязаны приводить к ProviderUnavailable, а не к другому исключению.
BROKEN_MODES = ("http500", "notjson", "notobject", "missing", "null", "empty", "notstring", "badinner", "innerlist")


@contextmanager
def provider_env(base_url, model="stub-model", timeout="5"):
    keys = {"AI_PROVIDER": "ollama", "OLLAMA_BASE_URL": base_url, "OLLAMA_MODEL": model, "OLLAMA_TIMEOUT": timeout}
    before = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ProviderContractTests(unittest.TestCase):
    """Прямые вызовы провайдера: разбор конверта и аварийные случаи."""

    @classmethod
    def setUpClass(cls):
        cls.stub = OllamaStub()
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.stub.close()

    def provider(self, timeout=5):
        return OllamaProvider(self.stub.base_url, "stub-model", timeout)

    def test_valid_answer_is_returned_as_a_dict(self):
        self.stub.set_mode("ok")
        analysis = self.provider().analyze([], "Nike 43")
        self.assertIsInstance(analysis, dict)
        self.assertEqual(analysis["filters"]["brand"], "Nike")

    def test_every_broken_mode_raises_provider_unavailable(self):
        for mode in BROKEN_MODES:
            with self.subTest(mode=mode):
                self.stub.set_mode(mode)
                with self.assertRaises(ProviderUnavailable):
                    self.provider().analyze([], "Nike 43")

    def test_http_error_is_provider_unavailable(self):
        self.stub.set_mode("http500")
        with self.assertRaises(ProviderUnavailable) as caught:
            self.provider().analyze([], "Nike 43")
        self.assertIn("transport", str(caught.exception))

    def test_timeout_is_provider_unavailable(self):
        self.stub.set_mode("hang")
        with self.assertRaises(ProviderUnavailable):
            self.provider(timeout=1).analyze([], "Nike 43")
        self.stub.set_mode("ok")

    def test_connection_error_is_provider_unavailable(self):
        with self.assertRaises(ProviderUnavailable):
            OllamaProvider(unreachable_base_url(), "stub-model", 2).analyze([], "Nike 43")

    def test_extra_fields_are_flagged_by_the_gate(self):
        """Само поле провайдер не выдумывает: лишнее поле — уже нарушение контракта."""
        self.stub.set_mode("extrafields")
        analysis = self.provider().analyze([], "Nike 43")
        result = validate_analysis(self.db, analysis)
        self.assertIn(REASON_UNEXPECTED_KEY, [item.reason for item in result.rejections])
        self.assertTrue(result.contract_broken)

    # --- настройки окружения -------------------------------------------------

    def test_typo_in_timeout_does_not_break_provider_creation(self):
        with provider_env(self.stub.base_url, timeout="не число"):
            provider = get_provider()
            self.assertIsNotNone(provider)
            self.assertEqual(provider.timeout, 60.0)

    def test_zero_timeout_falls_back_to_a_sane_value(self):
        with provider_env(self.stub.base_url, timeout="0"):
            self.assertEqual(get_provider().timeout, 60.0)

    def test_provider_is_none_when_switched_off_or_model_missing(self):
        with provider_env(self.stub.base_url, model=""):
            self.assertIsNone(get_provider())
        with provider_env(self.stub.base_url):
            os.environ["AI_PROVIDER"] = "none"
            self.assertIsNone(get_provider())

    def test_env_url_is_used(self):
        with provider_env(self.stub.base_url):
            self.assertEqual(get_provider().base_url, self.stub.base_url)


class ProviderFailureOverHttpTests(unittest.TestCase):
    """Сбой провайдера через настоящий /api/chat: не 500 и всегда безопасный fallback."""

    @classmethod
    def setUpClass(cls):
        cls.stub = OllamaStub()
        ensure_catalog_schema()
        cls.db = SessionLocal()
        seed_database(cls.db)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.stub.close()

    def post(self, session_id, message, language="ru"):
        return self.client.post("/api/chat", json={"session_id": session_id, "message": message, "language": language})

    def test_every_broken_mode_keeps_the_endpoint_alive(self):
        with provider_env(self.stub.base_url):
            for mode in BROKEN_MODES:
                with self.subTest(mode=mode):
                    self.stub.set_mode(mode)
                    response = self.post(f"contract-{mode}", "Nike 43")
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertEqual(payload["mode"], "fallback")
                    self.assertTrue(payload["products"])
                    self.assertTrue(all(product["brand"] == "Nike" for product in payload["products"]))

    def test_timeout_over_http_keeps_the_endpoint_alive(self):
        with provider_env(self.stub.base_url, timeout="1"):
            self.stub.set_mode("hang")
            response = self.post("contract-timeout", "Nike 43")
            self.stub.set_mode("ok")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "fallback")

    def test_connection_error_over_http_keeps_the_endpoint_alive(self):
        with provider_env(unreachable_base_url()):
            response = self.post("contract-conn", "Nike 43")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "fallback")

    def test_internal_errors_are_not_shown_to_the_user(self):
        with provider_env(self.stub.base_url):
            self.stub.set_mode("notjson")
            reply = self.post("contract-leak", "Nike 43").json()["reply"]
        for leak in ("Traceback", "URLError", "json", "ProviderUnavailable", "500"):
            self.assertNotIn(leak, reply)

    def test_provider_contribution_marks_the_mode(self):
        """Режим ollama означает, что разбор модели повлиял на выдачу, а не что он был вызван."""
        with provider_env(self.stub.base_url):
            self.stub.set_mode("evidence")
            payload = self.post("contract-ok", "Хочу тёмные кроссовки 43").json()
        self.assertEqual(payload["mode"], "ollama")
        self.assertTrue(all(product["color"] == "Black" for product in payload["products"]))

    def test_agreement_with_the_parser_stays_fallback(self):
        """Если модель сказала то же, что и парсер, выдачу определил парсер: это fallback."""
        with provider_env(self.stub.base_url):
            self.stub.set_mode("ok")
            payload = self.post("contract-agree", "Nike 43").json()
        self.assertEqual(payload["mode"], "fallback")
        self.assertTrue(all(product["brand"] == "Nike" for product in payload["products"]))

    def test_contract_violation_falls_back_instead_of_trusting_the_answer(self):
        """Лишнее поле в ответе модели — отказ от всего разбора, а не частичное доверие."""
        with provider_env(self.stub.base_url):
            self.stub.set_mode("extrafields")
            payload = self.post("contract-extra", "Nike 43").json()
        self.assertEqual(payload["mode"], "fallback")
        self.assertNotIn("водонепроницаемые", payload["reply"])

    def test_unexpected_provider_exception_does_not_break_the_request(self):
        """Даже неожиданный тип исключения обязан дать fallback, а не 500."""
        class Exploding:
            def analyze(self, history, message):
                raise RuntimeError("provider exploded")

        with patch("app.services.chat.get_provider", return_value=Exploding()):
            response = self.post("contract-explode", "Nike 43")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "fallback")

    def test_provider_setup_failure_does_not_break_the_request(self):
        with patch("app.services.chat.get_provider", side_effect=ValueError("broken config")):
            response = self.post("contract-setup", "Nike 43")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "fallback")


if __name__ == "__main__":
    unittest.main()
