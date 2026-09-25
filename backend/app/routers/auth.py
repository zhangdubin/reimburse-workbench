"""登录 / 登出 / 当前用户 / 改密码。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models as m
from .. import security as sec
from ..database import get_db

router = APIRouter(prefix="/api/auth", tags=["认证"])

# 登录失败的审计记录里带上尝试的用户名，便于排查撞库
def _audit(db: Session, *, action: str, user: m.AppUser | None, request: Request,
           status_code: int, detail: str | None = None) -> None:
    db.add(
        m.AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            role=user.role if user else None,
            action=action,
            entity="auth",
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            ip=sec._client_ip(request),
            detail=detail,
        )
    )
    db.commit()


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


def user_out(u: m.AppUser) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "name": u.name,
        "role": u.role,
        "employee_id": u.employee_id,
        "employee_name": u.employee.name if u.employee else None,
        "department_id": u.department_id,
        "department_name": u.employee.department.name if u.employee and u.employee.department else None,
        "approval_level": u.approval_level,
        "active": u.active,
        "must_change_password": u.must_change_password,
        "last_login_at": u.last_login_at.isoformat(sep=" ", timespec="seconds") if u.last_login_at else None,
    }


@router.post("/login", response_model=dict)
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    user = db.query(m.AppUser).filter(m.AppUser.username == payload.username.strip()).first()
    # 用户不存在与密码错误返回同一句话，避免被用来枚举账号
    if not user or not sec.verify_password(payload.password, user.password_hash):
        _audit(db, action="登录失败", user=user, request=request, status_code=401,
               detail=f"用户名 {payload.username!r} 认证失败")
        raise HTTPException(401, "用户名或密码错误")
    if not user.active:
        _audit(db, action="登录失败", user=user, request=request, status_code=403, detail="账号已停用")
        raise HTTPException(403, "账号已停用，请联系管理员")

    token = sec.create_session(db, user, request)
    _audit(db, action="登录", user=user, request=request, status_code=200)
    return {
        "token": token,
        "expires_in": sec.TOKEN_TTL_HOURS * 3600,
        "user": user_out(user),
    }


@router.post("/logout", response_model=dict)
def logout(
    request: Request,
    user: Annotated[m.AppUser, Depends(sec.current_user)],
    token: Annotated[str | None, Depends(sec.current_token)],
    db: Session = Depends(get_db),
):
    if token:
        sec.revoke_session(db, token)
    _audit(db, action="登出", user=user, request=request, status_code=200)
    return {"ok": True}


@router.get("/me", response_model=dict)
def me(user: Annotated[m.AppUser, Depends(sec.current_user)], db: Session = Depends(get_db)):
    from ..approvals import approval_summary

    return {"user": user_out(user), "approval": approval_summary(db)}


@router.post("/password", response_model=dict)
def change_password(
    payload: PasswordIn,
    request: Request,
    user: Annotated[m.AppUser, Depends(sec.current_user)],
    db: Session = Depends(get_db),
):
    if not sec.verify_password(payload.old_password, user.password_hash):
        _audit(db, action="改密码失败", user=user, request=request, status_code=400, detail="原密码错误")
        raise HTTPException(400, "原密码不正确")
    sec.check_password_strength(payload.new_password)
    user.password_hash = sec.hash_password(payload.new_password)
    user.must_change_password = False
    # 改完密码把该账号所有会话踢掉，包括当前这条——防止旧 token 继续可用
    sec.revoke_all_sessions(db, user.id)
    _audit(db, action="修改密码", user=user, request=request, status_code=200)
    return {"ok": True, "message": "密码已更新，请重新登录"}


@router.get("/server-time")
def server_time():
    """前端用来对时，顺带作为未鉴权探活接口。"""
    return {"now": datetime.now().isoformat(sep=" ", timespec="seconds")}
