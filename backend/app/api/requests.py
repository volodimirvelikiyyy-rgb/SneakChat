from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..admin_auth import require_admin
from ..database import get_db
from ..schemas import ContactRequestAdminRead, ContactRequestCreate, ContactRequestResponse, ContactRequestStatusUpdate
from ..services.requests import create_contact_request, delete_contact_request, list_contact_requests, update_contact_request_status

router = APIRouter(prefix="/api/requests", tags=["contact requests"])


@router.post("", response_model=ContactRequestResponse, status_code=201)
def submit_contact_request(payload: ContactRequestCreate, db: Session = Depends(get_db)):
    request = create_contact_request(db, payload)
    if request is None:
        raise HTTPException(status_code=400, detail="Selected size is unavailable")
    return {"id": request.id, "message": "Заявка принята"}


@router.get("", response_model=list[ContactRequestAdminRead], dependencies=[Depends(require_admin)])
def read_contact_requests(db: Session = Depends(get_db)):
    return list_contact_requests(db)


@router.patch("/{request_id}/status", response_model=ContactRequestResponse, dependencies=[Depends(require_admin)])
def change_contact_request_status(request_id: int, payload: ContactRequestStatusUpdate, db: Session = Depends(get_db)):
    request = update_contact_request_status(db, request_id, payload.status)
    if request is None:
        raise HTTPException(status_code=404, detail="Contact request not found")
    return {"id": request.id, "message": "Статус обновлён"}


@router.delete("/{request_id}", status_code=204, dependencies=[Depends(require_admin)])
def remove_contact_request(request_id: int, db: Session = Depends(get_db)):
    if not delete_contact_request(db, request_id):
        raise HTTPException(status_code=404, detail="Contact request not found")
    return Response(status_code=204)
