import json
import logging

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..models import ChatLog

logger = logging.getLogger("uvicorn.error")


def record_chat_log(db: Session, session_id: str, message: str, language: str, result: dict) -> None:
    """Stores tester chat turns without affecting the customer-facing answer."""
    try:
        products = result.get("products") or []
        names = ", ".join(f"{product.get('brand', '')} {product.get('name', '')}".strip() for product in products)
        log = ChatLog(
            session_id=session_id,
            language=language,
            user_message=message,
            assistant_reply=result.get("reply", ""),
            product_count=len(products),
            product_names=names,
            needs_clarification=bool(result.get("needs_clarification")),
            mode=result.get("mode", "fallback"),
            filters_json=json.dumps(result.get("filters") or {}, ensure_ascii=False),
            suggestions_json=json.dumps(result.get("suggestions") or [], ensure_ascii=False),
        )
        db.add(log)
        db.commit()
    except Exception as error:  # noqa: BLE001 - logging must never break the chat.
        db.rollback()
        logger.warning("chat log write failed: %s: %s", type(error).__name__, error)


def _json_value(raw: str, fallback):
    try:
        return json.loads(raw or "")
    except json.JSONDecodeError:
        return fallback


def list_chat_logs(db: Session, limit: int = 200) -> list[dict]:
    logs = db.query(ChatLog).order_by(desc(ChatLog.created_at), desc(ChatLog.id)).limit(limit).all()
    return [
        {
            "id": log.id,
            "created_at": log.created_at.isoformat(timespec="minutes") if log.created_at else "",
            "session_id": log.session_id,
            "language": log.language,
            "user_message": log.user_message,
            "assistant_reply": log.assistant_reply,
            "product_count": log.product_count,
            "product_names": log.product_names,
            "needs_clarification": log.needs_clarification,
            "mode": log.mode,
            "filters": _json_value(log.filters_json, {}),
            "suggestions": _json_value(log.suggestions_json, []),
        }
        for log in logs
    ]
