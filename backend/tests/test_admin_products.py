"""End-to-end admin contract checks using an explicitly isolated SQLite file."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_DATABASE_URL = os.environ.get("SNEAKCHAT_DATABASE_URL", "")
if not TEST_DATABASE_URL:
    raise RuntimeError("Set SNEAKCHAT_DATABASE_URL to a temporary SQLite database before running tests")
if not Path(tempfile.gettempdir()).as_posix().casefold() in TEST_DATABASE_URL.replace("\\", "/").casefold():
    raise RuntimeError("Admin tests refuse to run unless SNEAKCHAT_DATABASE_URL points inside the system temp directory")

from app import database
from app.admin_auth import make_auth_record
from app.api import admin as admin_api
from app.main import app
from app.seed import seed_database


PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nmNwAAAAASUVORK5CYII=")


def asgi_request(method: str, target: str, *, body: bytes = b"", headers: dict[str, str] | None = None,
                 cookie: str = "", client_host: str = "127.0.0.1") -> tuple[int, dict[str, str], bytes]:
    split = urlsplit(target)
    path = split.path or "/"
    request_headers = {"host": "127.0.0.1:8001", "content-length": str(len(body)), **(headers or {})}
    if cookie:
        request_headers["cookie"] = cookie
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": split.query.encode(), "root_path": "",
        "headers": [(key.lower().encode(), value.encode()) for key, value in request_headers.items()],
        "client": (client_host, 12345), "server": ("127.0.0.1", 8001), "state": {},
    }
    sent = False
    messages: list[dict] = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    response_headers = {
        key.decode().lower(): value.decode()
        for item in messages if item["type"] == "http.response.start"
        for key, value in item.get("headers", [])
    }
    content = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return status, response_headers, content


def request_json(method: str, path: str, payload: dict | None = None, *, cookie: str = "", headers: dict | None = None,
                 client_host: str = "127.0.0.1"):
    raw = json.dumps(payload).encode() if payload is not None else b""
    request_headers = {"content-type": "application/json", **(headers or {})}
    status, response_headers, content = asgi_request(method, path, body=raw, headers=request_headers,
                                                      cookie=cookie, client_host=client_host)
    try:
        result = json.loads(content or b"null")
    except ValueError:
        result = content.decode(errors="replace")
    return status, response_headers, result


class AdminProductWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sneakchat-admin-workflow-")
        self.root = Path(self.temp.name)
        self.auth_path = self.root / "admin_auth.json"
        self.auth_path.write_text(json.dumps(make_auth_record("LocalAdmin-Test-Password-42")), encoding="utf-8")
        self.previous_auth = os.environ.get("SNEAKCHAT_ADMIN_AUTH_FILE")
        os.environ["SNEAKCHAT_ADMIN_AUTH_FILE"] = str(self.auth_path)
        self.previous_upload = admin_api.UPLOAD_DIR
        admin_api.UPLOAD_DIR = self.root / "uploads"

        # Start with a clean disposable schema and seed only this temporary database.
        for table in ("product_images", "variant_attributes", "product_attributes", "contact_requests", "chat_logs", "variants", "products", "attributes"):
            if table in database.inspect(database.engine).get_table_names():
                with database.engine.begin() as connection:
                    connection.execute(text(f'DELETE FROM "{table}"'))
        database.ensure_catalog_schema()
        with database.SessionLocal() as db:
            seed_database(db)

    def tearDown(self):
        admin_api.UPLOAD_DIR = self.previous_upload
        if self.previous_auth is None:
            os.environ.pop("SNEAKCHAT_ADMIN_AUTH_FILE", None)
        else:
            os.environ["SNEAKCHAT_ADMIN_AUTH_FILE"] = self.previous_auth
        self.temp.cleanup()

    def test_owner_can_manage_product_and_gallery_without_exposing_hidden_items(self):
        status, _, body = request_json("GET", "/api/admin/products")
        self.assertEqual(status, 401)
        status, _, _ = request_json("GET", "/api/requests")
        self.assertEqual(status, 401)
        status, _, _ = request_json("GET", "/api/chat/logs")
        self.assertEqual(status, 401)
        status, _, _ = request_json("GET", "/api/admin/products", client_host="192.0.2.10")
        self.assertEqual(status, 403)

        status, headers, login = request_json("POST", "/api/admin/login", {"password": "LocalAdmin-Test-Password-42"})
        self.assertEqual(status, 200, login)
        cookie = headers["set-cookie"].split(";", 1)[0]
        self.assertIn("httponly", headers["set-cookie"].lower())
        status, _, public = request_json("GET", "/api/products")
        self.assertEqual(status, 200)
        self.assertEqual(len(public), 28)

        payload = {
            "brand": "Test Brand", "name": "Local Sample", "color": "Blue", "price": 120,
            "currency": "USD",
            "description": "Temporary admin test item", "gender": "unisex", "purpose": "walking",
            "features": ["lightweight"], "characteristics": [{"name": "Material", "value": "mesh"}],
            "variants": [{"size": 38, "stock": 2}, {"size": 39, "stock": 1}], "status": "draft",
        }
        status, _, product = request_json("POST", "/api/admin/products", payload, cookie=cookie)
        self.assertEqual(status, 201, product)
        product_id = product["id"]
        self.assertEqual(product["status"], "draft")
        self.assertEqual(len(product["variants"]), 2)

        payload.update(price=135.5, currency="EUR", description="Updated in the local admin form", status="published")
        status, _, updated = request_json("PUT", f"/api/admin/products/{product_id}", payload, cookie=cookie)
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["price"], 135.5)
        self.assertEqual(updated["currency"], "EUR")
        self.assertEqual(updated["description"], "Updated in the local admin form")
        status, _, euro_results = request_json("GET", "/api/products/search?max_price=200&currency=EUR")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"] == product_id and item["currency"] == "EUR" for item in euro_results))
        status, _, usd_results = request_json("GET", "/api/products/search?max_price=200&currency=USD")
        self.assertEqual(status, 200)
        self.assertFalse(any(item["id"] == product_id for item in usd_results))

        payload["currency"] = "UAH"
        status, _, updated = request_json("PUT", f"/api/admin/products/{product_id}", payload, cookie=cookie)
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["price"], 135.5)
        self.assertEqual(updated["currency"], "UAH")
        payload["currency"] = "USD"
        status, _, updated = request_json("PUT", f"/api/admin/products/{product_id}", payload, cookie=cookie)
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["price"], 135.5)
        self.assertEqual(updated["currency"], "USD")
        status, _, persisted = request_json("GET", f"/api/admin/products/{product_id}", cookie=cookie)
        self.assertEqual((persisted["price"], persisted["currency"]), (135.5, "USD"))

        status, _, invalid_currency = request_json("PUT", f"/api/admin/products/{product_id}", {**payload, "currency": "GBP"}, cookie=cookie)
        self.assertEqual(status, 422)

        photo_ids = []
        for _ in range(3):
            status, _, photo = asgi_request("POST", f"/api/admin/products/{product_id}/photos", body=PNG_1PX,
                                              headers={"content-type": "image/png"}, cookie=cookie)
            self.assertEqual(status, 201, photo)
            photo_ids.append(json.loads(photo)["id"])
        status, _, invalid = asgi_request("POST", f"/api/admin/products/{product_id}/photos", body=b"not-an-image",
                                            headers={"content-type": "image/png"}, cookie=cookie)
        self.assertEqual(status, 415, invalid)

        status, _, _ = request_json("PATCH", f"/api/admin/products/{product_id}/photos/{photo_ids[1]}",
                                     {"is_primary": True, "crop_scale": 1.4, "crop_x": 12, "crop_y": -5}, cookie=cookie)
        self.assertEqual(status, 200)
        status, _, ordered = request_json("PUT", f"/api/admin/products/{product_id}/photos/order",
                                          {"photo_ids": [photo_ids[2], photo_ids[1], photo_ids[0]]}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual([photo["id"] for photo in ordered], [photo_ids[2], photo_ids[1], photo_ids[0]])
        self.assertTrue(ordered[0]["is_primary"])
        status, _, main_photo = request_json("PATCH", f"/api/admin/products/{product_id}/photos/{photo_ids[1]}",
                                             {"is_primary": True}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(main_photo["is_primary"])
        status, _, ordered = request_json("GET", f"/api/admin/products/{product_id}", cookie=cookie)
        self.assertEqual([photo["id"] for photo in ordered["photos"]], [photo_ids[1], photo_ids[2], photo_ids[0]])
        self.assertEqual(ordered["photos"][0]["crop_scale"], 1.4)
        retained_file = admin_api.UPLOAD_DIR / ordered["photos"][0]["filename"]
        status, _, removed = request_json("DELETE", f"/api/admin/products/{product_id}/photos/{photo_ids[0]}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(removed["original_retained"])
        self.assertTrue(retained_file.is_file())

        status, _, _ = request_json("PATCH", f"/api/admin/products/{product_id}/status", {"status": "hidden"}, cookie=cookie)
        self.assertEqual(status, 200)
        status, _, _ = request_json("GET", f"/api/products/{product_id}")
        self.assertEqual(status, 404)
        status, _, public = request_json("GET", "/api/products")
        self.assertEqual(status, 200)
        self.assertNotIn(product_id, [item["id"] for item in public])
        variant_id = updated["variants"][0]["id"]
        status, _, _ = request_json("POST", "/api/requests", {
            "product_id": product_id, "variant_id": variant_id, "name": "Test", "contact": "@test",
        })
        self.assertEqual(status, 400)

        status, _, _ = request_json("PATCH", f"/api/admin/products/{product_id}/status", {"status": "published"}, cookie=cookie)
        self.assertEqual(status, 200)
        status, _, public_product = request_json("GET", f"/api/products/{product_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(public_product["photos"]), 2)
        status, _, _ = request_json("DELETE", f"/api/admin/products/{product_id}", cookie=cookie)
        self.assertEqual(status, 200)
        status, _, _ = request_json("GET", f"/api/products/{product_id}")
        self.assertEqual(status, 404)
        status, _, restore = request_json("PATCH", f"/api/admin/products/{product_id}/status", {"status": "hidden"}, cookie=cookie)
        self.assertEqual(status, 200)

        database.ensure_catalog_schema()
        with database.SessionLocal() as db:
            from app.models import Product
            persisted = db.get(Product, product_id)
            self.assertEqual(persisted.description, "Updated in the local admin form")
            self.assertEqual(float(persisted.base_price), 135.5)
            self.assertEqual(persisted.admin_status, "hidden")
            self.assertEqual(db.query(Product).count(), 29)


if __name__ == "__main__":
    unittest.main()
