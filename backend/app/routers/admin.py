"""管理后台接口：账号管理、系统参数、操作审计。全部限管理员。"""

from __future__ import annotations

from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .. import approvals as ap
from .. import models as m
from .. import security as sec
from ..database import get_db
from .auth import user_out

router = APIRouter(prefix="/api", tags=["管理后台"])

# ------------------------------------------------------------------ 账号管理
class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=32)
    role: str = m.ROLE_APPLICANT
    employee_id: int | None = None
    approval_level: int = Field(m.LEVEL_NONE, ge=0, le=2)
    active: bool = True
    password: str | None = Field(None, max_length=128)


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    employee_id: int | None = None
    approval_level: int | None = Field(None, ge=0, le=2)
    active: bool | None = None


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=1, max_length=128)


def _validate_role(role: str) -> None:
    if role not in m.ROLES:
        raise HTTPException(400, f"角色必须是 {'/'.join(m.ROLES)} 之一")


@router.get("/users", response_model=dict)
def list_users(
    q: str | None = None,
    role: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    query = db.query(m.AppUser).options(selectinload(m.AppUser.employee).selectinload(m.Employee.department))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(m.AppUser.username.like(like), m.AppUser.name.like(like)))
    if role:
        query = query.filter(m.AppUser.role == role)
    total = query.count()
    rows = query.order_by(m.AppUser.id.asc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [user_out(u) for u in rows],
        "roles": m.ROLES,
    }


@router.post("/users", response_model=dict, status_code=201)
def create_user(
    payload: UserIn,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    _validate_role(payload.role)
    if not payload.password:
        raise HTTPException(400, "请设置初始密码")
    sec.check_password_strength(payload.password)
    obj = m.AppUser(
        username=payload.username.strip(),
        name=payload.name,
        role=payload.role,
        employee_id=payload.employee_id,
        approval_level=payload.approval_level if payload.role == m.ROLE_APPROVER else m.LEVEL_NONE,
        active=payload.active,
        password_hash=sec.hash_password(payload.password),
        # 管理员代设的密码，强制首次登录后修改
        must_change_password=True,
    )
    db.add(obj)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "用户名已存在")
    db.refresh(obj)
    _write_audit(db, admin, request, "创建账号", "user", obj.id, detail=f"{obj.username} / {obj.role}")
    return user_out(obj)


@router.put("/users/{oid}", response_model=dict)
def update_user(
    oid: int,
    payload: UserPatch,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    obj = db.get(m.AppUser, oid)
    if not obj:
        raise HTTPException(404, "账号不存在")
    data = payload.model_dump(exclude_unset=True)
    if "role" in data:
        _validate_role(data["role"])
    # 不允许把最后一个可用管理员降级或停用，否则没人能进后台了
    will_lose_admin = (data.get("role") not in (None, m.ROLE_ADMIN)) or (data.get("active") is False)
    if obj.role == m.ROLE_ADMIN and will_lose_admin and _active_admin_count(db) <= 1:
        raise HTTPException(400, "系统至少需要保留一个启用的管理员账号")

    changes = []
    for k, v in data.items():
        if k == "approval_level" and data.get("role", obj.role) != m.ROLE_APPROVER:
            v = m.LEVEL_NONE
        if getattr(obj, k) != v:
            changes.append(f"{k}: {getattr(obj, k)} -> {v}")
        setattr(obj, k, v)
    if obj.role != m.ROLE_APPROVER:
        obj.approval_level = m.LEVEL_NONE
    db.commit()
    db.refresh(obj)
    if data.get("active") is False:
        sec.revoke_all_sessions(db, obj.id)
    _write_audit(db, admin, request, "修改账号", "user", obj.id, detail="; ".join(changes) or "无变化")
    return user_out(obj)


@router.post("/users/{oid}/reset-password", response_model=dict)
def reset_password(
    oid: int,
    payload: ResetPasswordIn,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    obj = db.get(m.AppUser, oid)
    if not obj:
        raise HTTPException(404, "账号不存在")
    sec.check_password_strength(payload.new_password)
    obj.password_hash = sec.hash_password(payload.new_password)
    obj.must_change_password = True
    db.commit()
    sec.revoke_all_sessions(db, obj.id)
    _write_audit(db, admin, request, "重置密码", "user", obj.id, detail=obj.username)
    return {"ok": True, "message": "密码已重置，该账号的登录状态已全部失效"}


@router.delete("/users/{oid}")
def delete_user(
    oid: int,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    obj = db.get(m.AppUser, oid)
    if not obj:
        raise HTTPException(404, "账号不存在")
    if obj.id == admin.id:
        raise HTTPException(400, "不能删除当前登录的账号")
    if obj.role == m.ROLE_ADMIN and _active_admin_count(db) <= 1:
        raise HTTPException(400, "系统至少需要保留一个启用的管理员账号")
    db.delete(obj)
    db.commit()
    _write_audit(db, admin, request, "删除账号", "user", oid, detail=obj.username)
    return {"ok": True, "deleted": oid}


def _active_admin_count(db: Session) -> int:
    return (
        db.query(func.count(m.AppUser.id))
        .filter(m.AppUser.role == m.ROLE_ADMIN, m.AppUser.active.is_(True))
        .scalar()
        or 0
    )


# ------------------------------------------------------------------ 系统参数
class SettingIn(BaseModel):
    value: str


@router.get("/settings", response_model=dict)
def list_settings(
    _user: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN, m.ROLE_FINANCE)),
    db: Session = Depends(get_db),
):
    ap.ensure_default_settings(db)
    rows = db.query(m.Setting).order_by(m.Setting.key).all()
    # 只下发本页可管理的参数（DEFAULT_SETTINGS）。setting 表里还有 jev_* 等外部配置，
    # 它们的管理入口在「AI 设置 → Jev 决策网关」（带密钥掩码）；一旦漏进来，
    # 前端「其他」组会把 URL/密钥渲染成数字框（浏览器报 cannot be parsed 且值被清空），
    # 且 PUT 会被下面的白名单 400 拒绝——保存必失败。
    return {
        "items": [
            {"key": r.key, "value": r.value, "remark": r.remark,
             "updated_at": r.updated_at.isoformat(sep=" ", timespec="seconds") if r.updated_at else None}
            for r in rows if r.key in ap.DEFAULT_SETTINGS
        ],
        "approval": ap.approval_summary(db),
    }


@router.put("/settings/{key}", response_model=dict)
def update_setting(
    key: str,
    payload: SettingIn,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if key not in ap.DEFAULT_SETTINGS:
        raise HTTPException(400, f"未知参数 {key}")
    old = ap.get_setting(db, key)
    row = ap.set_setting(db, key, payload.value)
    _write_audit(db, admin, request, "修改参数", "setting", key, detail=f"{old} -> {payload.value}")
    return {"key": row.key, "value": row.value, "remark": row.remark}


# ------------------------------------------------------------------ 审计日志
@router.get("/audit-logs", response_model=dict)
def list_audit_logs(
    q: str | None = None,
    username: str | None = None,
    action: str | None = None,
    entity: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    query = db.query(m.AuditLog)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(m.AuditLog.detail.like(like), m.AuditLog.path.like(like), m.AuditLog.username.like(like))
        )
    if username:
        query = query.filter(m.AuditLog.username == username)
    if action:
        query = query.filter(m.AuditLog.action == action)
    if entity:
        query = query.filter(m.AuditLog.entity == entity)
    if date_from:
        query = query.filter(m.AuditLog.created_at >= f"{date_from} 00:00:00")
    if date_to:
        # 含当天：用「次日零点」做上界，避免 23:59 之后的记录被漏掉
        end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(m.AuditLog.created_at < end.isoformat(sep=" ", timespec="seconds"))

    total = query.count()
    rows = (
        query.order_by(m.AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    actions = [a for (a,) in db.query(m.AuditLog.action).distinct().order_by(m.AuditLog.action).all()]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "actions": actions,
        "items": [
            {
                "id": r.id,
                "username": r.username,
                "role": r.role,
                "action": r.action,
                "entity": r.entity,
                "entity_id": r.entity_id,
                "method": r.method,
                "path": r.path,
                "status_code": r.status_code,
                "ip": r.ip,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(sep=" ", timespec="seconds") if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/audit-logs/batch-delete", response_model=dict)
def batch_delete_audit_logs(
    payload: dict,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """批量删除审计日志（管理员）。删除动作本身也会留一条记录。"""
    ids = [int(i) for i in (payload.get("ids") or []) if str(i).isdigit()]
    if not ids:
        raise HTTPException(400, "请先勾选要删除的日志")
    if len(ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 条，请分批操作")
    n = db.query(m.AuditLog).filter(m.AuditLog.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    _write_audit(db, admin, request, "批量删除审计日志", "audit_log",
                 detail=f"删除 {n} 条，ids={ids[:20]}{'…' if len(ids) > 20 else ''}")
    return {"ok": True, "deleted": int(n)}


@router.post("/audit-logs/purge", response_model=dict)
def purge_audit_logs(
    request: Request,
    payload: dict | None = None,
    before_days: int | None = Query(None, ge=0, description="只清理 N 天前的日志，不传=全清"),
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """清理审计日志。默认全清；给了 before_days 就只清更早的，便于保留近期痕迹。"""
    keyword = "清空审计日志"
    got = ((payload or {}).get("confirm") or "").strip()
    if got != keyword:
        raise HTTPException(400, f"危险操作：请在确认框中原样输入「{keyword}」")

    query = db.query(m.AuditLog)
    scope_text = "全部"
    if before_days is not None:
        cut = (datetime.now() - timedelta(days=before_days)).isoformat(sep=" ", timespec="seconds")
        query = query.filter(m.AuditLog.created_at < cut)
        scope_text = f"{before_days} 天前"
    n = query.delete(synchronize_session=False)
    db.commit()
    _write_audit(db, admin, request, "清理审计日志", "audit_log", detail=f"清理范围：{scope_text}，删除 {n} 条")
    return {"ok": True, "deleted": int(n), "scope": scope_text}


def _write_audit(db: Session, user: m.AppUser | None, request: Request, action: str,
                 entity: str | None = None, entity_id=None, detail: str | None = None) -> None:
    db.add(
        m.AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            role=user.role if user else None,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            method=request.method,
            path=request.url.path,
            status_code=200,
            ip=sec._client_ip(request),
            detail=detail,
        )
    )
    db.commit()
