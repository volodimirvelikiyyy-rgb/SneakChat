import json
import os
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..catalog_rules import lexicon


class ProviderUnavailable(Exception):
    pass


class ConversationProvider(Protocol):
    def analyze(self, history: list[dict], message: str) -> dict: ...


def build_prompt(history: list[dict], message: str) -> str:
    """Промпт для модели.

    Словарь значений берётся из `catalog_rules.lexicon()`, то есть из того же источника,
    что использует детерминированный парсер: модель и парсер опираются на один словарь.
    """
    recent_history = history[-6:]
    return (
        "You analyze Russian or Ukrainian sneaker-store messages. Return valid JSON only, with exactly three keys: "
        "intent, filters, evidence. "
        # Список допустимых значений даётся словами, а не внутри примера: иначе модель копирует
        # саму строку со знаками «|» как значение поля и весь ответ перестаёт быть годным.
        "The value of intent is one word, chosen from: search_products, check_stock, product_details, "
        "compare_products, recommendation, clarification. "
        'Shape to follow, with values replaced by your own: {"intent":"search_products",'
        '"filters":{"brand":null,"color":null,"size":null,"min_price":null,"max_price":null,"currency":null,"gender":null,"use_case":null,"feature":null},'
        '"evidence":{"brand":null,"color":null,"size":null,"gender":null,"use_case":null,"feature":null}}. '
        "An unknown filter is null: never use \"N/A\", \"none\", \"-\", \"unknown\" or an empty string as a value, "
        "and never put several words into one field. "
        f"{lexicon()}\n"
        "For every categorical filter you fill in, including currency, copy into evidence the exact substring of the latest message that proves it — "
        "use the user's own words, do not translate or reword them. A categorical filter without such a substring in the latest message must stay null. "
        "Extract only constraints explicitly stated in the latest message; conversation history is context, not a source of product facts. "
        "Use JSON numbers for size and prices. currency is USD, EUR or UAH only when the user names that currency or its symbol; never calculate or invent rates. "
        "Leave currency null for an unmarked budget; the application applies the shopper's selected display currency and its cached Frankfurter reference rates. "
        "'до 120' is max_price 120. 'примерно за сотку' or 'приблизно за сотню' means 80 to 120. "
        "Return only the three keys intent, filters and evidence: an extra key makes the whole answer invalid. "
        "You are not allowed to state any fact about a product. Never mention a price, a stock level, a size, a colour, "
        "a purpose or a property of any sneaker, and never claim that such a fact is true. The catalogue is the only "
        "source of product facts, and the reply text is written by the application, not by you. "
        "A value that is not in the vocabulary above does not exist: leave the filter null instead of guessing. "
        f"Conversation history: {json.dumps(recent_history, ensure_ascii=False)}\n"
        f"Latest user message: {message}"
    )


class OllamaProvider:
    def __init__(self, base_url: str, model: str, timeout: float = 60):
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout

    def _post(self, payload: dict) -> str:
        """Тело ответа Ollama. Любая транспортная проблема — это ProviderUnavailable."""
        request = Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, OSError) as error:
            # URLError покрывает и HTTP-ошибки (5xx), и недоступную модель: urlopen возбуждает
            # HTTPError, который наследуется от URLError.
            raise ProviderUnavailable(f"transport: {error}") from error

    def analyze(self, history: list[dict], message: str) -> dict:
        """Разбор сообщения моделью.

        Контракт проверяется на каждом шаге: ответ сервера — объект с текстовым полем
        `response`, а этот текст — JSON-объект. Модель может ответить не тем, чем обещала,
        поэтому каждая ошибка контракта превращается в `ProviderUnavailable`, а не в
        исключение другого типа: вызывающий код обязан уметь откатиться на детерминированный
        разбор при любом поведении модели.
        """
        raw = self._post({
            "model": self.model, "prompt": build_prompt(history, message), "format": "json", "stream": False,
            "options": {"num_ctx": 2048, "num_predict": 256, "temperature": 0.3},
        })
        try:
            envelope = json.loads(raw)
        except ValueError as error:
            raise ProviderUnavailable("envelope is not JSON") from error
        if not isinstance(envelope, dict):
            raise ProviderUnavailable("envelope is not an object")
        body = envelope.get("response")
        if not isinstance(body, str) or not body.strip():
            # Модель ответила пусто, числом, null или объектом — разбирать нечего.
            raise ProviderUnavailable("missing response text")
        try:
            analysis = json.loads(body)
        except ValueError as error:
            raise ProviderUnavailable("analysis is not JSON") from error
        if not isinstance(analysis, dict):
            raise ProviderUnavailable("analysis is not an object")
        return analysis

    def respond(self, candidates: list[str]) -> str:
        """Выбирает одну готовую backend-формулировку, не сочиняя факты о каталоге."""
        prompt = (
            "Return JSON with exactly one key, reply. Copy reply exactly from the supplied options. "
            "Do not add, remove, or alter any words, numbers, prices, sizes, product facts, or claims. "
            f"Options: {json.dumps(candidates, ensure_ascii=False)}"
        )
        raw = self._post({
            "model": self.model, "prompt": prompt, "format": "json", "stream": False,
            "options": {"num_ctx": 2048, "num_predict": 96, "temperature": 0.3},
        })
        try:
            envelope = json.loads(raw)
            body = envelope.get("response") if isinstance(envelope, dict) else None
            result = json.loads(body) if isinstance(body, str) else None
        except (TypeError, ValueError) as error:
            raise ProviderUnavailable("reply is not valid JSON") from error
        reply = result.get("reply") if isinstance(result, dict) else None
        if not isinstance(result, dict) or set(result) != {"reply"}:
            raise ProviderUnavailable("reply does not match the response contract")
        if not isinstance(reply, str) or reply not in candidates:
            raise ProviderUnavailable("reply is not an approved backend candidate")
        return reply


def get_provider() -> ConversationProvider | None:
    """Провайдер из окружения или None, если он выключен.

    Настройки читаются защищённо: опечатка в переменной окружения не должна ломать запрос
    пользователя, поэтому непонятная настройка означает «провайдера нет».
    """
    if os.getenv("AI_PROVIDER", "").lower() != "ollama":
        return None
    model = os.getenv("OLLAMA_MODEL", "").strip()
    if not model:
        return None
    try:
        timeout = float(os.getenv("OLLAMA_TIMEOUT", "60"))
    except ValueError:
        timeout = 60.0
    if timeout <= 0:
        timeout = 60.0
    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    parsed_url = urlsplit(base_url)
    if parsed_url.scheme != "http" or parsed_url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return None
    return OllamaProvider(base_url, model, timeout)
