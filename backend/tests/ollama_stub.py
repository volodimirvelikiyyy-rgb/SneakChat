"""Локальная HTTP-заглушка Ollama для проверки контракта провайдера.

Заглушка повторяет реальный API: `POST /api/generate` возвращает объект с текстовым полем
`response`. Поведение задаётся строкой-режимом, поэтому проверяется настоящий транспорт
(`urlopen`), а не подмена функции: провайдер конструируется как в проде и ходит по HTTP.

Режимы:
    ok           — корректный ответ с разбором запроса;
    http500      — сервер отвечает ошибкой;
    notjson      — тело ответа не JSON;
    notobject    — тело ответа JSON, но не объект;
    missing      — в конверте нет поля response;
    null         — response = null;
    empty        — response = "";
    notstring    — response — число;
    badinner     — response содержит не JSON;
    innerlist    — response содержит JSON-массив;
    extrafields  — разбор запроса с лишним полем верхнего уровня;
    evidence     — разбор с формулировкой, которой нет в словаре парсера;
    hang         — сервер не отвечает (для проверки timeout).
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALID_ANALYSIS = {"intent": "search_products", "filters": {"brand": "Nike", "size": 43}, "evidence": {"brand": "nike"}}
EXTRA_ANALYSIS = {**VALID_ANALYSIS, "reply": "эти Nike водонепроницаемые"}
# Модель знает слово, которого нет в словаре парсера: «тёмные» → Black. Только на таком
# ответе видно, что разбор модели действительно повлиял на выдачу.
EVIDENCE_ANALYSIS = {"intent": "search_products", "filters": {"color": "Black"}, "evidence": {"color": "тёмные"}}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — имя задано базовым классом
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        mode = self.server.mode
        if mode == "hang":
            time.sleep(self.server.hang_seconds)
        if mode == "http500":
            self._send(500, b"upstream is broken")
            return
        body = {
            "ok": json.dumps({"response": json.dumps(VALID_ANALYSIS)}).encode(),
            "extrafields": json.dumps({"response": json.dumps(EXTRA_ANALYSIS)}).encode(),
            "evidence": json.dumps({"response": json.dumps(EVIDENCE_ANALYSIS)}).encode(),
            "notjson": b"<html>not json at all</html>",
            "notobject": b"[1, 2, 3]",
            "missing": json.dumps({"model": "stub"}).encode(),
            "null": json.dumps({"response": None}).encode(),
            "empty": json.dumps({"response": ""}).encode(),
            "notstring": json.dumps({"response": 43}).encode(),
            "badinner": json.dumps({"response": "not json"}).encode(),
            "innerlist": json.dumps({"response": "[1, 2]"}).encode(),
            "hang": b"{}",
        }.get(mode, json.dumps({"response": json.dumps(VALID_ANALYSIS)}).encode())
        self._send(200, body)

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Клиент оборвал соединение (например, по timeout) — для заглушки это норма.
            return

    def log_message(self, *args):  # тишина в выводе тестов
        return


class OllamaStub:
    """Запускает заглушку на свободном порту и управляет её режимом."""

    def __init__(self, mode="ok", hang_seconds=3.0):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.mode = mode
        self.server.hang_seconds = hang_seconds
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def set_mode(self, mode):
        self.server.mode = mode

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def unreachable_base_url() -> str:
    """Адрес, по которому никто не слушает: проверка ошибки соединения."""
    probe = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = probe.server_address[1]
    probe.server_close()
    return f"http://127.0.0.1:{port}"
