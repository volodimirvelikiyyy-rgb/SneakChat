"""Минимальный HTTP-сервер поверх ASGI для E2E-тестов интерфейса.

Только стандартная библиотека: uvicorn в окружении не установлен, а проверять интерфейс нужно
через настоящий HTTP и настоящий backend, а не через mock.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, "..", "..", "backend"))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, ".python-packages"))

from app.database import SessionLocal, ensure_catalog_schema  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_database  # noqa: E402

ensure_catalog_schema()
_session = SessionLocal()
try:
    seed_database(_session)
finally:
    _session.close()


async def handle(reader, writer):
    try:
        request_line = await reader.readline()
        if not request_line:
            return
        parts = request_line.decode("latin-1").split()
        if len(parts) < 3:
            return
        method, target = parts[0], parts[1]
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        body = await reader.readexactly(length) if length else b""
        path, _, query = target.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": target.encode("latin-1"),
            "query_string": query.encode("latin-1"),
            "root_path": "",
            "headers": [(name.encode("latin-1"), value.encode("latin-1")) for name, value in headers.items()],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8011),
        }
        messages = []
        body_consumed = False

        async def receive():
            nonlocal body_consumed
            if not body_consumed:
                body_consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)

        async def send(message):
            if message["type"] == "http.response.start":
                messages.append(("start", message["status"], message.get("headers", [])))
            elif message["type"] == "http.response.body":
                messages.append(("body", message.get("body", b""), message.get("more_body", False)))

        await app(scope, receive, send)

        status = 200
        payload = b""
        content_type = "application/json"
        for message in messages:
            if message[0] == "start":
                status = message[1]
                for name, value in message[2]:
                    if name.lower() == b"content-type":
                        content_type = value.decode("latin-1")
            else:
                payload += message[1]
        head = (
            f"HTTP/1.1 {status} X\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + payload)
        await writer.drain()
    except Exception:  # noqa: BLE001 — тестовый сервер не должен падать на одном запросе
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def main(port):
    server = await asyncio.start_server(handle, "127.0.0.1", port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8011))
