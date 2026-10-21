from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..admin_auth import require_admin
from ..database import get_db
from ..schemas import ChatLogAdminRead, ChatRequest, ChatResponse
from ..services.chat import chat
from ..services.chat_logs import list_chat_logs, record_chat_log

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("/logs", response_model=list[ChatLogAdminRead], dependencies=[Depends(require_admin)])
def read_chat_logs(db: Session = Depends(get_db)):
    return list_chat_logs(db)


@router.post("", response_model=ChatResponse)
def send_message(payload: ChatRequest, db: Session = Depends(get_db)):
    message = payload.message.strip()
    result = chat(db, payload.session_id, message, payload.language, payload.display_currency)
    record_chat_log(db, payload.session_id, message, payload.language, result)
    return result
