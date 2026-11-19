import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.access import user_access_payload
from app.core.config import settings
from app.core.deps import MIRROR_OWNER_ONLY, OWNER_ROLES, check_hub, get_current_user
from app.core.rbac import HOME_BY_ROLE
from app.core.security import create_token, decode_token, hash_password, password_ok, verify_password
from app.db.session import get_db
from app.models.users import User
from app.schemas.common import ChangePasswordIn, RefreshIn, TokenOut, UserOut
from app.services.utils import audit

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form.username).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Inactive user")
    if settings.mirror_mode and user.role not in OWNER_ROLES:
        raise HTTPException(status_code=403, detail=MIRROR_OWNER_ONLY)
    check_hub(user)
    return _token_out(user)


def _token_out(user: User) -> TokenOut:
    return TokenOut(
        access_token=create_token(str(user.id), token_type="access"),
        refresh_token=create_token(str(user.id), token_type="refresh"),
        user_id=user.id,
        role=user.role,
        home=HOME_BY_ROLE.get(user.role, "/app"),
        full_name=user.full_name,
        branch_id=user.branch_id,
    )


@router.post("/mirror-sso", response_model=TokenOut)
def mirror_sso(x_mirror_key: str = Header(default=""), db: Session = Depends(get_db)):
    """Clinic copy only: a session for the clinic's owner account, handed to the
    hospital owner by the main instance (POST /sync/clinic-session) so the Owner
    Panel can open clinic data without a second login."""
    key = settings.mirror_internal_key
    if not settings.mirror_mode or not key or not hmac.compare_digest(x_mirror_key.encode(), key.encode()):
        raise HTTPException(status_code=403, detail="Not allowed")
    user = (
        db.query(User)
        .filter(User.role == "hospital_admin", User.is_active == True)  # noqa: E712
        .order_by(User.id)
        .first()
    )
    if not user:
        raise HTTPException(status_code=404, detail="Clinic data has no owner account yet")
    return _token_out(user)


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("typ") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Inactive user")
    if settings.mirror_mode and user.role not in OWNER_ROLES:
        raise HTTPException(status_code=403, detail=MIRROR_OWNER_ONLY)
    check_hub(user)
    return _token_out(user)


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "department": user.department,
        "branch_id": user.branch_id,
        "specialty": user.specialty,
        "consultation_fee": user.consultation_fee,
        **user_access_payload(user),
    }


@router.post("/change-password")
def change_password(data: ChangePasswordIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not verify_password(data.current_password, user.hashed_password):
        raise HTTPException(400, "Current password is incorrect")
    if data.new_password == data.current_password:
        raise HTTPException(400, "New password must be different from the current one")
    if not password_ok(data.new_password):
        raise HTTPException(400, "Password must be at least 8 characters and mix 3 of: upper case, lower case, number, symbol")
    user.hashed_password = hash_password(data.new_password)
    audit(db, user.id, "change_password", "user", str(user.id))
    db.commit()
    return {"ok": True}
