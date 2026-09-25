"""认证与授权：密码哈希、会话 token、当前用户依赖、角色校验、数据可见范围。

设计取舍：
- 密码用标准库 hashlib.pbkdf2_hmac，不引入 bcrypt/passlib，避免生产镜像多一层编译依赖。
  存储格式 `pbkdf2_sha256$<轮数>$<盐>$<摘要hex>`，轮数写进哈希里，将来调高轮数不会让老密码失效。
- 会话表只存 token 的 SHA-256，库里落盘的不是明文 token；支持过期与强制下线（revoked）。
- token 同时支持 `Authorization: Bearer` 与 Cookie，前端用前者。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session

from . import models as m
from .database import get_db

PBKDF2_ROUNDS = int(os.getenv("PBKDF2_ROUNDS", "120000"))
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "12"))
COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "wb_token")

MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "8"))

# 数据可见范围中的「什么都看不到」条件。
# 用 SQLAlchemy 的 false() 而不是 id < 0，语义明确且跨库安全。
_NO_ACCESS = false()


# ------------------------------------------------------------------ 密码
def hash_password(password: str, *, rounds: int | None = None) -> str:
    rounds = rounds or PBKDF2_ROUNDS
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, rounds_s, salt, digest = stored.split("$")
        rounds = int(rounds_s)
    except (ValueError, AttributeError):
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds)
    # compare_digest 防时序侧信道
    return hmac.compare_digest(dk.hex(), digest)


def check_password_strength(password: str) -> None:
    """生产环境的密码底线校验，不满足直接抛 400。"""
    if len(password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"密码长度至少 {MIN_PASSWORD_LEN} 位")
    kinds = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if kinds < 2:
        raise HTTPException(400, "密码需至少包含字母、数字、符号中的两类")


# ------------------------------------------------------------------ 会话
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _client_ip(request: Request) -> str | None:
    # 生产一般挂在反向代理后面，优先取 X-Forwarded-For 的第一跳
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def create_session(db: Session, user: m.AppUser, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        m.UserSession(
            token_hash=_hash_token(token),
            user_id=user.id,
            ip=_client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:255] or None,
            expires_at=datetime.now() + timedelta(hours=TOKEN_TTL_HOURS),
        )
    )
    user.last_login_at = datetime.now()
    db.commit()
    return token


def revoke_session(db: Session, token: str) -> None:
    row = db.query(m.UserSession).filter(m.UserSession.token_hash == _hash_token(token)).first()
    if row:
        row.revoked = True
        db.commit()


def revoke_all_sessions(db: Session, user_id: int) -> int:
    """改密码 / 停用账号时把该用户的会话全部踢掉。"""
    n = (
        db.query(m.UserSession)
        .filter(m.UserSession.user_id == user_id, m.UserSession.revoked.is_(False))
        .update({"revoked": True}, synchronize_session=False)
    )
    db.commit()
    return n


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(COOKIE_NAME)


# ------------------------------------------------------------------ 依赖
def current_user(request: Request, db: Session = Depends(get_db)) -> m.AppUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(401, "未登录", headers={"WWW-Authenticate": "Bearer"})
    row = (
        db.query(m.UserSession)
        .filter(m.UserSession.token_hash == _hash_token(token))
        .first()
    )
    if not row or row.revoked:
        raise HTTPException(401, "登录已失效，请重新登录", headers={"WWW-Authenticate": "Bearer"})
    if row.expires_at < datetime.now():
        raise HTTPException(401, "登录已过期，请重新登录", headers={"WWW-Authenticate": "Bearer"})
    user = db.get(m.AppUser, row.user_id)
    if not user or not user.active:
        raise HTTPException(401, "账号已停用", headers={"WWW-Authenticate": "Bearer"})
    return user


def current_token(request: Request) -> str | None:
    return _extract_token(request)


def require_roles(*roles: str):
    """生成「只允许指定角色」的依赖。"""

    def dep(user: m.AppUser = Depends(current_user)) -> m.AppUser:
        if user.role not in roles:
            raise HTTPException(403, f"当前角色「{user.role}」无权执行该操作")
        return user

    return dep


# ------------------------------------------------------------------ 数据可见范围
def scope_conds(user: m.AppUser) -> list:
    """返回一组作用在 Reimbursement 上的可见性条件。

    - 管理员 / 财务：全部数据
    - 审批人：本部门
    - 申请人：自己提交的
    未绑定员工或部门的账号拿不到对应数据，用恒假条件兜底而不是放行。
    """
    if user.role in (m.ROLE_ADMIN, m.ROLE_FINANCE):
        return []
    if user.role == m.ROLE_APPROVER:
        did = user.department_id
        return [m.Reimbursement.department_id == did] if did else [_NO_ACCESS]
    if user.employee_id:
        return [m.Reimbursement.applicant_id == user.employee_id]
    return [_NO_ACCESS]


def can_view(user: m.AppUser, r: m.Reimbursement) -> bool:
    if user.role in (m.ROLE_ADMIN, m.ROLE_FINANCE):
        return True
    if user.role == m.ROLE_APPROVER:
        return r.department_id is not None and r.department_id == user.department_id
    return user.employee_id is not None and r.applicant_id == user.employee_id


def ensure_can_view(user: m.AppUser, r: m.Reimbursement) -> None:
    if not can_view(user, r):
        raise HTTPException(403, "无权访问该报销单")


def current_scope(user: m.AppUser = Depends(current_user)) -> list:
    """FastAPI 依赖版的数据范围，便于在路由签名里直接声明。"""
    return scope_conds(user)


def invoice_scope_conds(user: m.AppUser) -> list:
    """发票的可见范围。

    发票可能未关联报销单（散票，还在流转途中），所以不能只按报销单过滤：
    - 管理员 / 财务：全部
    - 其他角色：关联到「自己看得见的报销单」的发票，外加自己登记的散票
    """
    if user.role in (m.ROLE_ADMIN, m.ROLE_FINANCE):
        return []
    if user.role == m.ROLE_APPROVER:
        did = user.department_id
        if not did:
            return [_NO_ACCESS]
        sub = select(m.Reimbursement.id).where(m.Reimbursement.department_id == did)
    elif user.employee_id:
        sub = select(m.Reimbursement.id).where(m.Reimbursement.applicant_id == user.employee_id)
    else:
        return [_NO_ACCESS]
    return [
        or_(
            m.Invoice.reimbursement_id.in_(sub),
            m.Invoice.created_by == user.username,
        )
    ]


def current_invoice_scope(user: m.AppUser = Depends(current_user)) -> list:
    return invoice_scope_conds(user)
