from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.deps import require
from app.db.session import get_db
from app.models.users import User
from app.services import queue_service

# Staff side: the nurse moves each doctor's queue along. Reception can see it too.
router = APIRouter(prefix="/opd-queue", tags=["queue"])
STAFF = require("nursing", "front_desk", "queue")

# Public side: no login. The patient's QR page and the waiting-room TV. Neither
# returns patient names/phones/UHIDs — only queue numbers and doctor names.
public_router = APIRouter(prefix="/public", tags=["public"])
NOINDEX = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}


class CallNextIn(BaseModel):
    branch_id: int
    doctor_id: int | None = None


def _branch(user: User, branch_id: int) -> int:
    return user.branch_id or branch_id


@router.get("/today")
def today(branch_id: int, db: Session = Depends(get_db), user: User = Depends(STAFF)):
    board = queue_service.staff_board(db, _branch(user, branch_id))
    db.commit()  # keeps the old-token clean-up
    return board


@router.post("/call-next")
def call_next(data: CallNextIn, db: Session = Depends(get_db), user: User = Depends(STAFF)):
    t = queue_service.call_next(db, branch_id=_branch(user, data.branch_id), doctor_id=data.doctor_id)
    db.commit()
    return {"called": t.number if t else None}


def _act(fn, token_id: int, branch_id: int, db: Session, user: User):
    try:
        t = fn(db, token_id=token_id, branch_id=_branch(user, branch_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return {"id": t.id, "number": t.number, "status": t.status}


@router.post("/{token_id}/call")
def call(token_id: int, branch_id: int, db: Session = Depends(get_db), user: User = Depends(STAFF)):
    return _act(queue_service.call_token, token_id, branch_id, db, user)


@router.post("/{token_id}/recall")
def recall(token_id: int, branch_id: int, db: Session = Depends(get_db), user: User = Depends(STAFF)):
    return _act(queue_service.recall, token_id, branch_id, db, user)


@router.post("/{token_id}/skip")
def skip(token_id: int, branch_id: int, db: Session = Depends(get_db), user: User = Depends(STAFF)):
    return _act(queue_service.skip, token_id, branch_id, db, user)


@router.post("/{token_id}/requeue")
def requeue(token_id: int, branch_id: int, db: Session = Depends(get_db), user: User = Depends(STAFF)):
    return _act(queue_service.requeue, token_id, branch_id, db, user)


@router.post("/{token_id}/done")
def done(token_id: int, branch_id: int, db: Session = Depends(get_db), user: User = Depends(STAFF)):
    return _act(queue_service.finish, token_id, branch_id, db, user)


@public_router.get("/queue/{code}")
def public_ticket(code: str, response: Response, db: Session = Depends(get_db)):
    response.headers.update(NOINDEX)
    data = queue_service.public_ticket(db, code)
    if not data:
        # A token printed by a site server (its QR link points here, the cloud).
        from app.api.v1.sync import site_ticket

        data = site_ticket(code)
    if not data:
        raise HTTPException(404, "Token not found")
    return data


@public_router.get("/queue-display/{branch_id}")
def public_display(branch_id: int, response: Response, db: Session = Depends(get_db)):
    response.headers.update(NOINDEX)
    return queue_service.display_board(db, branch_id)
