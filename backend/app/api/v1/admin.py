from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.access import (
    can_user,
    ALL_FEATURE_KEYS,
    COUNTER_DEFINITIONS,
    COUNTER_KEYS,
    COUNTER_TAB_DEFINITIONS,
    ROLE_COUNTER_DEFAULTS,
    dump_json_list,
    role_default_features,
)
from app.core import clinic_view
from app.core.deps import get_current_user, require
from app.core.rbac import ROLES
from app.db.session import get_db
from app.models.ipd import Bed, Ward
from app.models.org import Branch
from app.models.users import User
from app.schemas.admin import (
    BedCreateIn,
    BedOut,
    BedUpdateIn,
    BranchCreateIn,
    BranchUpdateIn,
    CapitalAssetCreateIn,
    CapitalAssetOut,
    ResetPasswordIn,
    StaffCreateIn,
    StaffOut,
    StaffUpdateIn,
    WardCreateIn,
    WardOut,
    WardUpdateIn,
)
from app.schemas.common import BranchOut
from app.services.admin_service import (
    create_bed,
    delete_bed,
    delete_ward,
    create_branch,
    create_capital_asset,
    create_staff,
    create_ward,
    delete_capital_asset,
    list_beds,
    list_capital_assets,
    list_staff,
    list_wards,
    reset_staff_password,
    update_bed,
    update_branch,
    update_staff,
    update_ward,
)

router = APIRouter(prefix="/admin", tags=["admin"])

# No role in PERMS grants "__admin__" — only super_admin/hospital_admin (which bypass
# the permission check entirely in require()) can reach these endpoints.
ADMIN_ONLY = "__admin__"

# Roles that may reach the /admin/staff and ward/bed endpoints at all.
# super_admin/hospital_admin see and manage every branch; branch_admin is
# scoped to its own branch only (enforced per-endpoint below, not via the
# generic PERMS/can() mechanism, since "same branch" can't be expressed as a
# flat permission string) and can never touch an admin-tier account
# (super_admin/hospital_admin/branch_admin), even one in its own branch.
OWNER_ROLES = ("super_admin", "hospital_admin")
ADMIN_TIER_ROLES = (*OWNER_ROLES, "branch_admin")
BRANCH_SCOPED_ADMIN_ROLES = (*OWNER_ROLES, "branch_admin")


def require_branch_scoped_admin():
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in BRANCH_SCOPED_ADMIN_ROLES:
            raise HTTPException(403, "Not allowed")
        return user

    return checker


def require_ward_manager():
    """Ward & Bed setup: admins, or staff given the Management counter."""
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role in BRANCH_SCOPED_ADMIN_ROLES or can_user(user, "management") or can_user(user, "wards"):
            return user
        raise HTTPException(403, "Not allowed")

    return checker


def _scope_branch_id(user: User) -> int | None:
    """None for super_admin/hospital_admin (unscoped); the user's own branch
    otherwise (branch_admin, or staff with the Management counter)."""
    return None if user.role in OWNER_ROLES else user.branch_id


# Counters a branch_admin may not hand out: the Admin Panel itself. Management
# is fine — for non-admin staff it only gives Doctors, Test/Service Catalog and
# Ward & Bed (Staff Accounts and Capital Assets stay admin-only).
BRANCH_ADMIN_UNGRANTABLE = {"admin"}


def _grantable_counters(user: User) -> set[str]:
    return COUNTER_KEYS - BRANCH_ADMIN_UNGRANTABLE if user.role == "branch_admin" else set(COUNTER_KEYS)


def _apply_access(staff: User, user: User, counters: list[str] | None, features: list[str] | None) -> None:
    """Store a custom counter/tab list on staff, or clear it back to the role
    defaults when what was picked is exactly the role default."""
    if counters is None and features is None:
        return
    allowed = _grantable_counters(user)
    features = [f for f in (features or []) if f in ALL_FEATURE_KEYS and f.split(".", 1)[0] in allowed]
    counters = sorted({c for c in (counters or []) if c in allowed} | {f.split(".", 1)[0] for f in features})
    if counters == sorted(ROLE_COUNTER_DEFAULTS.get(staff.role, [])) and sorted(features) == role_default_features(staff.role):
        counters, features = [], []
    staff.allowed_counters = dump_json_list(counters)
    staff.allowed_features = dump_json_list(sorted(features))


@router.get("/access-meta")
def admin_access_meta(user: User = Depends(require_branch_scoped_admin())):
    """Counter/tab options for the Staff Accounts access picker."""
    allowed = _grantable_counters(user)
    return {
        "counters": [c for c in COUNTER_DEFINITIONS if c["key"] in allowed],
        "counter_tabs": {k: v for k, v in COUNTER_TAB_DEFINITIONS.items() if k in allowed},
        "role_defaults": {
            role: {
                "counters": [c for c in ROLE_COUNTER_DEFAULTS.get(role, []) if c in allowed],
                "features": [f for f in role_default_features(role) if f.split(".", 1)[0] in allowed],
            }
            for role in ROLES
        },
    }


@router.get("/staff", response_model=list[StaffOut])
def admin_list_staff(db: Session = Depends(get_db), user: User = Depends(require_branch_scoped_admin())):
    return list_staff(db, branch_id=_scope_branch_id(user))


@router.post("/staff", response_model=StaffOut)
def admin_create_staff(
    data: StaffCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_branch_scoped_admin()),
):
    branch_id = data.branch_id
    if user.role == "branch_admin":
        if data.role in ADMIN_TIER_ROLES:
            raise HTTPException(403, "Branch admin cannot create admin-tier accounts")
        branch_id = user.branch_id  # force own branch — ignore any branch_id sent by the client
    try:
        staff = create_staff(
            db,
            username=data.username,
            password=data.password,
            full_name=data.full_name,
            role=data.role,
            branch_id=branch_id,
            phone=data.phone,
            email=data.email,
            employee_code=data.employee_code,
            user_id=user.id,
        )
        _apply_access(staff, user, data.allowed_counters, data.allowed_features)
        db.commit()
        db.refresh(staff)
        return staff
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/staff/{staff_id}", response_model=StaffOut)
def admin_update_staff(
    staff_id: int,
    data: StaffUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_branch_scoped_admin()),
):
    sent = data.model_dump(exclude_unset=True)
    if user.role == "branch_admin":
        target = db.get(User, staff_id)
        if not target or target.branch_id != user.branch_id or target.role in ADMIN_TIER_ROLES:
            raise HTTPException(403, "Not allowed")
        if data.role is not None and data.role in ADMIN_TIER_ROLES:
            raise HTTPException(403, "Branch admin cannot assign admin-tier roles")
        if "branch_id" in sent and data.branch_id != user.branch_id:
            raise HTTPException(403, "Branch admin cannot move staff to another branch")
    try:
        staff = update_staff(
            db,
            staff_id,
            full_name=data.full_name,
            role=data.role,
            branch_id=data.branch_id,
            branch_id_set="branch_id" in sent,
            phone=data.phone,
            email=data.email,
            employee_code=data.employee_code,
            is_active=data.is_active,
            user_id=user.id,
        )
        _apply_access(staff, user, data.allowed_counters, data.allowed_features)
        db.commit()
        db.refresh(staff)
        return staff
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/staff/{staff_id}/reset-password", response_model=StaffOut)
def admin_reset_password(
    staff_id: int,
    data: ResetPasswordIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_branch_scoped_admin()),
):
    if user.role == "branch_admin":
        target = db.get(User, staff_id)
        if not target or target.branch_id != user.branch_id or target.role in ADMIN_TIER_ROLES:
            raise HTTPException(403, "Not allowed")
    try:
        staff = reset_staff_password(db, staff_id, data.new_password, user.id)
        db.commit()
        db.refresh(staff)
        return staff
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/branches", response_model=list[BranchOut])
def admin_list_branches(db: Session = Depends(get_db), _: User = Depends(require(ADMIN_ONLY))):
    return db.query(Branch).order_by(Branch.id).all()


@router.post("/branches", response_model=BranchOut)
def admin_create_branch(data: BranchCreateIn, db: Session = Depends(get_db), user: User = Depends(require(ADMIN_ONLY))):
    try:
        branch = create_branch(db, code=data.code, name=data.name, name_mm=data.name_mm, address=data.address, phone=data.phone, user_id=user.id)
        db.commit()
        db.refresh(branch)
        return branch
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/branches/{branch_id}", response_model=BranchOut)
def admin_update_branch(branch_id: int, data: BranchUpdateIn, db: Session = Depends(get_db), user: User = Depends(require(ADMIN_ONLY))):
    try:
        branch = update_branch(db, branch_id, name=data.name, name_mm=data.name_mm, address=data.address, phone=data.phone, user_id=user.id)
        db.commit()
        db.refresh(branch)
        return branch
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/wards", response_model=list[WardOut])
def admin_list_wards(branch_id: int, db: Session = Depends(get_db), user: User = Depends(require_ward_manager())):
    scope = _scope_branch_id(user)
    if scope is not None and branch_id != scope:
        raise HTTPException(403, "Not allowed")
    return list_wards(db, branch_id)


@router.post("/wards", response_model=WardOut)
def admin_create_ward(data: WardCreateIn, db: Session = Depends(get_db), user: User = Depends(require_ward_manager())):
    branch_id = data.branch_id
    scope = _scope_branch_id(user)
    if scope is not None:
        branch_id = scope  # force own branch — ignore any branch_id sent by the client
    try:
        ward = create_ward(db, branch_id=branch_id, name=data.name, category=data.category, floor=data.floor, user_id=user.id)
        db.commit()
        db.refresh(ward)
        return ward
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/wards/{ward_id}", response_model=WardOut)
def admin_update_ward(ward_id: int, data: WardUpdateIn, db: Session = Depends(get_db), user: User = Depends(require_ward_manager())):
    scope = _scope_branch_id(user)
    if scope is not None:
        ward = db.get(Ward, ward_id)
        if not ward or ward.branch_id != scope:
            raise HTTPException(403, "Not allowed")
    try:
        ward = update_ward(db, ward_id, name=data.name, category=data.category, floor=data.floor, user_id=user.id)
        db.commit()
        db.refresh(ward)
        return ward
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/wards/{ward_id}/beds", response_model=list[BedOut])
def admin_list_beds(ward_id: int, db: Session = Depends(get_db), user: User = Depends(require_ward_manager())):
    scope = _scope_branch_id(user)
    if scope is not None:
        ward = db.get(Ward, ward_id)
        if not ward or ward.branch_id != scope:
            raise HTTPException(403, "Not allowed")
    return list_beds(db, ward_id)


@router.post("/beds", response_model=BedOut)
def admin_create_bed(data: BedCreateIn, db: Session = Depends(get_db), user: User = Depends(require_ward_manager())):
    scope = _scope_branch_id(user)
    if scope is not None:
        ward = db.get(Ward, data.ward_id)
        if not ward or ward.branch_id != scope:
            raise HTTPException(403, "Not allowed")
    try:
        bed = create_bed(
            db,
            ward_id=data.ward_id,
            code=data.code,
            daily_rate=data.daily_rate,
            hourly_rate=data.hourly_rate,
            package_rate=data.package_rate,
            user_id=user.id,
        )
        db.commit()
        db.refresh(bed)
        return bed
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/beds/{bed_id}", response_model=BedOut)
def admin_update_bed(bed_id: int, data: BedUpdateIn, db: Session = Depends(get_db), user: User = Depends(require_ward_manager())):
    scope = _scope_branch_id(user)
    if scope is not None:
        bed = db.get(Bed, bed_id)
        ward = db.get(Ward, bed.ward_id) if bed else None
        if not bed or not ward or ward.branch_id != scope:
            raise HTTPException(403, "Not allowed")
    try:
        bed = update_bed(
            db,
            bed_id,
            code=data.code,
            daily_rate=data.daily_rate,
            hourly_rate=data.hourly_rate,
            package_rate=data.package_rate,
            user_id=user.id,
        )
        db.commit()
        db.refresh(bed)
        return bed
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/beds/{bed_id}")
def admin_delete_bed(bed_id: int, db: Session = Depends(get_db), user: User = Depends(require_ward_manager())):
    scope = _scope_branch_id(user)
    bed = db.get(Bed, bed_id)
    ward = db.get(Ward, bed.ward_id) if bed else None
    if scope is not None and (not ward or ward.branch_id != scope):
        raise HTTPException(403, "Not allowed")
    try:
        how = delete_bed(db, bed_id, user_id=user.id)
        db.commit()
        return {"ok": True, "how": how}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/wards/{ward_id}")
def admin_delete_ward(ward_id: int, db: Session = Depends(get_db), user: User = Depends(require_ward_manager())):
    scope = _scope_branch_id(user)
    ward = db.get(Ward, ward_id)
    if scope is not None and (not ward or ward.branch_id != scope):
        raise HTTPException(403, "Not allowed")
    try:
        how = delete_ward(db, ward_id, user_id=user.id)
        db.commit()
        return {"ok": True, "how": how}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/capital-assets", response_model=list[CapitalAssetOut])
def admin_list_capital_assets(branch_id: int, db: Session = Depends(get_db), _: User = Depends(require(ADMIN_ONLY))):
    site = clinic_view.site_for(branch_id)
    if site:  # Owner Panel: an offline site's assets, from its uploaded copy
        if not clinic_view.available(site):
            return []
        with clinic_view.clinic_session(site) as cdb:
            return [
                {**CapitalAssetOut.model_validate(a).model_dump(), "id": a.id + clinic_view.offset(site), "branch_id": branch_id}
                for a in list_capital_assets(cdb, clinic_view.LOCAL_ID)
            ]
    return list_capital_assets(db, branch_id)


@router.post("/capital-assets", response_model=CapitalAssetOut)
def admin_create_capital_asset(data: CapitalAssetCreateIn, db: Session = Depends(get_db), user: User = Depends(require(ADMIN_ONLY))):
    try:
        asset = create_capital_asset(
            db,
            branch_id=data.branch_id,
            name=data.name,
            category=data.category,
            cost=data.cost,
            purchased_on=data.purchased_on,
            notes=data.notes,
            user_id=user.id,
        )
        db.commit()
        db.refresh(asset)
        return asset
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/capital-assets/{asset_id}")
def admin_delete_capital_asset(asset_id: int, db: Session = Depends(get_db), user: User = Depends(require(ADMIN_ONLY))):
    try:
        delete_capital_asset(db, asset_id, user.id)
        db.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
