from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import AssistantSearchRequest, AssistantSearchResponse
from ..services.assistant import search_from_message

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


@router.post("/search", response_model=AssistantSearchResponse)
def assistant_search(payload: AssistantSearchRequest, db: Session = Depends(get_db)):
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message must not be empty")
    filters, products = search_from_message(db, message)
    if not filters:
        reply = "Укажите бренд, цвет, размер или бюджет. Например: «Белые Nike 43 размера до $120»."
    elif products:
        reply = f"Нашёл {len(products)} подходящих моделей. Показываю только товары с нужным размером в наличии."
    else:
        reply = "По этим условиям товаров нет. Попробуйте изменить цвет, размер или бюджет."
    return {"reply": reply, "applied_filters": filters, "products": products}
