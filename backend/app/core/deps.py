from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.access import can_user
from app.core.config import settings
from app.core.rbac import can
from app.core.security import decode_token
from app.db.session import get_db
from app.models.users import User

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
OWNER_ROLES = ("super_admin", "hospital_admin")
MIRROR_OWNER_ONLY = "Clinic data ကို Owner ပဲ ကြည့်လို့ရပါတယ်"
# Cloud hub (hub_mode): counters run on the site servers; only these use the cloud.
HUB_ROLES = OWNER_ROLES + ("warehouse", "executive")
HUB_STAFF_ONLY = "Cloud မှာ Owner နဲ့ Warehouse ပဲ ဝင်လို့ရပါတယ် — ဆေးရုံ / ဆေးခန်း server (LAN) ကို သုံးပါ"


# Site servers get their item list (and prices) from the cloud's item master.
CATALOG_MANAGED = "Item / ဈေးနှုန်းကို Cloud (Warehouse account) မှာ ထည့်/ပြင်ပါ — ဒီ server ကို အလိုလို ရောက်လာပါမယ်"


def ensure_catalog_editable() -> None:
    if settings.site:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CATALOG_MANAGED)


def check_hub(user) -> None:
    if settings.hub_mode and user.role not in HUB_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=HUB_STAFF_ONLY)


def get_current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)) -> User:
    payload = decode_token(token)
    if not payload or payload.get("typ") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user")
    if settings.mirror_mode and user.role not in OWNER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MIRROR_OWNER_ONLY)
    check_hub(user)
    return user


def require(*perms: str):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role in ("super_admin", "hospital_admin"):
            return user
        if not any(can_user(user, p) for p in perms):
            raise HTTPException(status_code=403, detail="Not allowed")
        return user

    return checker


def require_developer():
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role != "super_admin":
            raise HTTPException(status_code=403, detail="Developer access only")
        return user

    return checker
