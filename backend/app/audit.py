"""操作审计中间件。

在路由之外统一采集：这样以后新增接口不必记得手写审计，天然不会漏。
只记录**写操作**（POST/PUT/PATCH/DELETE）——GET 量大且无副作用，记了只会把表撑爆。
登录/登出由 auth 路由自己记录（那里能拿到「登录失败」这种中间件看不到的信息）。
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from . import models as m
from .database import SessionLocal
from .security import _client_ip, _hash_token

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# 这些路径自己写审计，中间件跳过，避免一条操作记两遍
SKIP_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/auth/password"}

# 路径片段 -> (动作, 实体名)。顺序敏感：更具体的放前面。
RULES: list[tuple[str, str, str]] = [
    ("/submit", "提交报销单", "reimbursement"),
    ("/withdraw", "撤回报销单", "reimbursement"),
    ("/approve", "审批通过", "reimbursement"),
    ("/reject", "审批驳回", "reimbursement"),
    ("/unpay", "撤销付款", "reimbursement"),
    ("/pay", "确认付款", "reimbursement"),
    ("/attach", "上传发票影像", "invoice"),
    ("/check", "发票查验", "invoice"),
    ("/link", "关联报销单", "invoice"),
    ("/reset-password", "重置密码", "user"),
]

RESOURCE_ACTIONS = {
    "reimbursements": ("报销单", "reimbursement"),
    "invoices": ("发票", "invoice"),
    "attachments": ("发票影像", "invoice"),
    "departments": ("部门", "department"),
    "employees": ("员工", "employee"),
    "categories": ("费用类型", "expense_category"),
    "customers": ("客户", "customer"),
    "projects": ("项目", "project"),
    "budgets": ("预算", "budget"),
    "users": ("账号", "user"),
    "settings": ("系统参数", "setting"),
}

VERB = {"POST": "新增", "PUT": "修改", "PATCH": "修改", "DELETE": "删除"}


def describe(method: str, path: str) -> tuple[str, str | None, str | None]:
    """把 方法+路径 翻译成人看得懂的审计条目。"""
    for frag, action, entity in RULES:
        if path.endswith(frag) or frag in path:
            return action, entity, None

    parts = [p for p in path.strip("/").split("/") if p]
    # /api/<resource>/<id>
    if len(parts) >= 2 and parts[0] == "api":
        resource = parts[1]
        oid = parts[2] if len(parts) > 2 and parts[2].isdigit() else None
        if resource in RESOURCE_ACTIONS:
            label, entity = RESOURCE_ACTIONS[resource]
            return f"{VERB.get(method, method)}{label}", entity, oid
        return f"{VERB.get(method, method)}{resource}", resource, oid
    return f"{VERB.get(method, method)}操作", None, None


def _resolve_user(db, request: Request):
    """中间件里没有依赖注入，按同样的规则从头部/ Cookie 反查一次登录态。"""
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.cookies.get("wb_token")
    if not token:
        return None
    row = db.query(m.UserSession).filter(m.UserSession.token_hash == _hash_token(token)).first()
    if not row or row.revoked:
        return None
    return db.get(m.AppUser, row.user_id)


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        path = request.url.path
        if request.method not in MUTATING or not path.startswith("/api/"):
            return response
        if path in SKIP_PATHS:
            return response

        # 审计失败绝不能影响业务响应
        db = SessionLocal()
        try:
            user = _resolve_user(db, request)
            action, entity, entity_id = describe(request.method, path)
            db.add(
                m.AuditLog(
                    user_id=user.id if user else None,
                    username=user.username if user else None,
                    role=user.role if user else None,
                    action=action,
                    entity=entity,
                    entity_id=entity_id,
                    method=request.method,
                    path=path[:255],
                    status_code=response.status_code,
                    ip=_client_ip(request),
                    detail=(f"{request.url.query}"[:500] if request.url.query else None),
                )
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()
        return response
