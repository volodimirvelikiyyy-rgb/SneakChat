"""Безопасное сохранение контекста диалога в существующей SQLite."""

import json
import threading
from weakref import WeakSet

from sqlalchemy.orm import Session

from ..models import ChatSession
from .products import get_product, serialize_product

_ready_engines: WeakSet = WeakSet()
_schema_lock = threading.Lock()
_DEFAULT_STATE = {
    "filters": {}, "products": [], "history": [], "shown_product_ids": [],
    "shopping": False, "browse": False, "skip_size": False, "similar_overlay": None,
    "clarification": {}, "comparison": None, "preferences": {}, "hard_constraints": {},
    "last_successful_product_ids": [],
    "dialogue": {},
}
_PERSISTED_KEYS = (
    "filters", "history", "shown_product_ids", "shopping", "browse", "skip_size", "similar_overlay",
    "clarification", "comparison", "preferences", "hard_constraints", "last_successful_product_ids",
    "dialogue",
)


def _ensure_schema(db: Session) -> None:
    engine = db.get_bind()
    with _schema_lock:
        if engine not in _ready_engines:
            ChatSession.__table__.create(bind=engine, checkfirst=True)
            _ready_engines.add(engine)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def load_session_state(db: Session, session_id: str) -> dict:
    """Загружает контекст и восстанавливает карточки только из текущего каталога."""
    _ensure_schema(db)
    row = db.get(ChatSession, session_id)
    if row is None:
        return {**_DEFAULT_STATE, "filters": {}, "products": [], "history": [], "shown_product_ids": [], "clarification": {}, "dialogue": {}}
    try:
        stored = json.loads(row.state_json)
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    state = {**_DEFAULT_STATE, **{key: stored[key] for key in _PERSISTED_KEYS if key in stored}}
    state["filters"] = dict(state["filters"]) if isinstance(state.get("filters"), dict) else {}
    state["clarification"] = dict(state["clarification"]) if isinstance(state.get("clarification"), dict) else {}
    state["dialogue"] = dict(state["dialogue"]) if isinstance(state.get("dialogue"), dict) else {}
    state["comparison"] = dict(state["comparison"]) if isinstance(state.get("comparison"), dict) else None
    state["preferences"] = dict(state["preferences"]) if isinstance(state.get("preferences"), dict) else {}
    state["hard_constraints"] = dict(state["hard_constraints"]) if isinstance(state.get("hard_constraints"), dict) else {}
    state["history"] = list(state["history"])[-12:] if isinstance(state.get("history"), list) else []
    state["shown_product_ids"] = list(state["shown_product_ids"]) if isinstance(state.get("shown_product_ids"), list) else []
    state["last_successful_product_ids"] = (
        list(state["last_successful_product_ids"])
        if isinstance(state.get("last_successful_product_ids"), list) else []
    )
    state["browse"] = bool(state.get("browse"))
    state["shopping"] = bool(state.get("shopping"))
    state["skip_size"] = bool(state.get("skip_size"))
    products = []
    stored_products = stored.get("products")
    product_ids = (
        [item.get("id") for item in stored_products if isinstance(item, dict)]
        if isinstance(stored_products, list)
        else stored.get("shown_product_ids", [])
    )
    for product_id in product_ids:
        try:
            product = get_product(db, int(product_id))
        except (TypeError, ValueError):
            continue
        if product is not None:
            products.append(serialize_product(product))
    state["products"] = products
    last_successful_products = []
    for product_id in state["last_successful_product_ids"]:
        try:
            product = get_product(db, int(product_id))
        except (TypeError, ValueError):
            continue
        if product is not None:
            last_successful_products.append(serialize_product(product))
    state["last_successful_products"] = last_successful_products
    return state


def save_session_state(db: Session, session_id: str, state: dict) -> None:
    """Сохраняет только диалоговые условия; снимки товаров используются лишь для восстановления ID."""
    _ensure_schema(db)
    payload = {key: _json_safe(state.get(key)) for key in _PERSISTED_KEYS}
    payload["history"] = (payload["history"] or [])[-12:]
    payload["products"] = [
        {"id": int(product["id"])}
        for product in state.get("products", []) if product.get("id") is not None
    ]
    payload["last_successful_product_ids"] = [
        int(product["id"])
        for product in state.get("last_successful_products", []) if product.get("id") is not None
    ]
    row = db.get(ChatSession, session_id)
    if row is None:
        row = ChatSession(session_id=session_id, state_json="{}")
        db.add(row)
    row.state_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    db.commit()
