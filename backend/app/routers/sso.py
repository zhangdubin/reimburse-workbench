"""单点登录：面向统一门户的三段流程（配置 / 发起 / 回调）都在这里。

接口分两块：
    /api/auth/sso/*    登录流程本身 —— 没有会话也能访问
    /api/admin/sso/*   身份源配置 —— 仅管理员

为什么登录回调不直接把 token 塞给前端：
  门户回调是浏览器重定向，URL 里任何东西都会进历史、进 referer、进代理日志。
  所以回调只写一枚一次性的 handover cookie，前端回来再 POST 换 token ——
  URL 全程干净，出现问题也不用担心 token 被复制走。

账户落地、角色映射、协议细节都在 app/sso.py，本模块只做 HTTP 编排。
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import crypt
from .. import models as m
from .. import security as sec
from .. import sso
from ..database import get_db
from .auth import user_out

router = APIRouter(prefix="/api/auth/sso", tags=["单点登录"])
admin_router = APIRouter(prefix="/api/admin/sso", tags=["单点登录配置"])

# handover cookie：回调后的短期凭据，180 秒内不换 token 就作废
HANDOVER_COOKIE = "wb_sso_handover"
HANDOVER_TTL = 180
PENDING_TTL = 300  # 门户那边登录页停留时间的上限


# ------------------------------------------------------------------ 工具
def _base(request: Request) -> str:
    """对外可访问的地址。

    门户要用它拼 redirect_uri，必须是浏览器真正访问到的那个域名。
    优先信任反代的 X-Forwarded-*（生产在 Nginx 后面），兜底再看 FastAPI 看到的地址。
    """
    env_base = (os.getenv("SSO_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if env_base:
        return env_base
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    host = host.split(",")[0].strip()
    return f"{proto}://{host}"


def _callback_url(request: Request, pid: int) -> str:
    return f"{_base(request)}/api/auth/sso/{pid}/callback"


def _stringify(value: str | None) -> str:
    return (value or "").strip()


def _provider_dict(p: m.SsoProvider, *, with_secret: bool = False, base: str = "") -> dict:
    out = {
        "id": p.id,
        "name": p.name,
        "protocol": p.protocol,
        "protocol_label": m.SSO_PROTOCOL_LABELS.get(p.protocol, p.protocol),
        "enabled": p.enabled,
        "issuer": _stringify(p.issuer),
        "authorize_url": _stringify(p.authorize_url),
        "token_url": _stringify(p.token_url),
        "userinfo_url": _stringify(p.userinfo_url),
        "validate_url": _stringify(p.validate_url),
        "jwks_url": _stringify(p.jwks_url),
        "logout_url": _stringify(p.logout_url),
        "client_id": _stringify(p.client_id),
        "scope": _stringify(p.scope),
        "use_pkce": p.use_pkce,
        "jwt_source": p.jwt_source or "query",
        "jwt_param": _stringify(p.jwt_param) or "token",
        "jwt_audience": _stringify(p.jwt_audience),
        "claim_subject": _stringify(p.claim_subject) or "sub",
        "claim_username": _stringify(p.claim_username),
        "claim_name": _stringify(p.claim_name),
        "claim_employee_no": _stringify(p.claim_employee_no),
        "claim_department": _stringify(p.claim_department),
        "claim_email": _stringify(p.claim_email),
        "claim_phone": _stringify(p.claim_phone),
        "claim_groups": _stringify(p.claim_groups),
        "auto_create": p.auto_create,
        "default_role": p.default_role,
        "role_map": sso.role_map(p),
        "sync_profile": p.sync_profile,
        "allow_local_login": p.allow_local_login,
        "bind_local_by_username": p.bind_local_by_username,
        "session_hours": p.session_hours,
        "verify_ssl": p.verify_ssl,
        "timeout_sec": p.timeout_sec,
        "remark": _stringify(p.remark),
        "created_at": p.created_at.isoformat(sep=" ", timespec="seconds") if p.created_at else None,
        "updated_at": p.updated_at.isoformat(sep=" ", timespec="seconds") if p.updated_at else None,
    }
    if base:
        out["callback_url"] = f"{base}/api/auth/sso/{p.id}/callback"
        out["login_url"] = f"{base}/api/auth/sso/{p.id}/login"
    if with_secret:
        out["client_secret_masked"] = crypt.mask(crypt.decrypt(p.client_secret_enc))
        out["jwt_secret_masked"] = crypt.mask(crypt.decrypt(p.jwt_secret_enc))
    return out


def _get_provider(db: Session, pid: int) -> m.SsoProvider:
    obj = db.get(m.SsoProvider, pid)
    if not obj:
        raise HTTPException(404, "身份源不存在")
    return obj


def _purge_expired(db: Session) -> None:
    db.query(m.SsoLoginState).filter(m.SsoLoginState.expires_at < datetime.now()).delete(
        synchronize_session=False
    )
    db.commit()


def _write_audit(db: Session, user: m.AppUser | None, request: Request, action: str,
                 entity: str | None = None, entity_id=None, detail: str | None = None,
                 status_code: int = 200) -> None:
    """写审计。

    没有复用 auth._audit：那个是给登录/登出用的简版（只支持 entity="auth"），
    SSO 这里还要区分身份源配置变更，自己写一份更省心。
    """
    db.add(
        m.AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            role=user.role if user else None,
            action=action,
            entity=entity or "auth",
            entity_id=str(entity_id) if entity_id is not None else None,
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            ip=sec._client_ip(request),
            detail=detail,
        )
    )
    db.commit()


def _fail_redirect(request: Request, message: str) -> RedirectResponse:
    """登录失败：回首页并带上原因，前端登录页负责把这句话显示出来。"""
    return RedirectResponse(
        url=f"{_base(request)}/?sso_error={quote(message[:200])}", status_code=302
    )


# ================================================================ 登录流程
@router.get("/providers", response_model=dict)
def public_providers(request: Request, db: Session = Depends(get_db)):
    """登录页要展示的快捷入口（未登录可访问，只给必要字段）。"""
    rows = (
        db.query(m.SsoProvider)
        .filter(m.SsoProvider.enabled.is_(True))
        .order_by(m.SsoProvider.id)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "protocol": r.protocol,
                "protocol_label": m.SSO_PROTOCOL_LABELS.get(r.protocol, r.protocol),
                "login_url": f"/api/auth/sso/{r.id}/login",
            }
            for r in rows
        ]
    }


@router.get("/{pid}/login")
def sso_login(pid: int, request: Request, db: Session = Depends(get_db)):
    """跳转到统一门户的登录页；JWT 直通模式则当场校验并进入 handover。"""
    p = _get_provider(db, pid)
    if not p.enabled:
        raise HTTPException(400, "该登录方式未启用")
    _purge_expired(db)
    params = dict(request.query_params)

    # JWT 直通：门户带着令牌直接进门，不需要先去门户那边绕一圈
    if p.protocol == m.PROTO_JWT:
        return _finish(request, db, p, params, request.headers, None)

    nonce = secrets.token_urlsafe(24)
    verifier = ""
    try:
        verifier, challenge = sso.new_pkce() if p.use_pkce else ("", "")
        target = sso.build_authorize_url(p, _callback_url(request, pid), nonce, challenge)
    except sso.SsoError as e:
        raise HTTPException(400, str(e)) from e

    db.add(
        m.SsoLoginState(
            nonce=nonce,
            provider_id=p.id,
            stage="pending",
            pkce_verifier=verifier or None,
            expires_at=datetime.now() + timedelta(seconds=PENDING_TTL),
        )
    )
    db.commit()
    return RedirectResponse(url=target, status_code=302)


@router.api_route("/{pid}/callback", methods=["GET", "POST"])
async def sso_callback(pid: int, request: Request, db: Session = Depends(get_db)):
    """门户回调。验证完毕后落到一次性的 handover cookie，再 302 回首页。"""
    p = _get_provider(db, pid)
    if not p.enabled:
        raise HTTPException(400, "该登录方式未启用")

    if request.method == "POST":
        form = await request.form()
        params = {k: v for k, v in form.items()}
    else:
        params = dict(request.query_params)

    if p.protocol == m.PROTO_JWT:
        return _finish(request, db, p, params, request.headers, None)

    state = params.get("state")
    row = None
    if state:
        row = db.get(m.SsoLoginState, str(state))
    if row is not None and (row.used or row.provider_id != p.id or row.expires_at < datetime.now()):
        _write_audit(db, action="SSO登录失败", user=None, request=request, status_code=400,
               detail=f"state 校验失败（{p.name}）")
        return _fail_redirect(request, "登录请求已失效，请重新发起")
    if row is None and p.protocol in (m.PROTO_OAUTH2, m.PROTO_OIDC):
        # OAuth2/OIDC 的 state 是防 CSRF 的：门户一定会原样带回。
        # 拿不到就说明这次回调不是我们自己发起的，直接拒。
        _write_audit(db, action="SSO登录失败", user=None, request=request, status_code=400,
               detail=f"回调缺少/不匹配 state（{p.name}）")
        return _fail_redirect(request, "登录请求缺少校验参数，请重新发起单点登录")
    # CAS 协议不带 state（只有一次性票据），放行 —— 票据本身不可复用
    return _finish(request, db, p, params, request.headers, row)


def _finish(request: Request, db: Session, p: m.SsoProvider, params: dict,
            headers, pending_row) -> RedirectResponse:
    """认证后的收尾：解析身份 → 落地账号 → 建会话 → 发 handover cookie。"""
    try:
        profile = sso.resolve_profile(
            p, params, redirect_uri=_callback_url(request, p.id),
            headers=headers, pkce_verifier=(pending_row.pkce_verifier if pending_row else "") or "",
        )
    except sso.SsoError as e:
        detail = dict(sso.last_error())
        _write_audit(db, action="SSO登录失败", user=None, request=request, status_code=401,
               detail=f"{p.name}: {e} | {json.dumps(detail, ensure_ascii=False)[:300]}")
        return _fail_redirect(request, str(e))

    if pending_row is not None:
        pending_row.used = True
        db.flush()

    try:
        user = sso.resolve_or_create_user(db, p, profile)
        hours = p.session_hours if p.session_hours and p.session_hours > 0 else None
        token = sec.create_session(db, user, request, hours=hours, sso_provider_id=p.id)
    except sso.SsoError as e:
        db.rollback()
        _write_audit(db, action="SSO登录失败", user=None, request=request, status_code=403,
               detail=f"{p.name}: {e}")
        return _fail_redirect(request, str(e))

    nonce = secrets.token_urlsafe(24)
    db.add(
        m.SsoLoginState(
            nonce=nonce,
            provider_id=p.id,
            stage="ready",
            profile_json=json.dumps(profile, ensure_ascii=False),
            username_attempt=(user.username or "")[:64],
            expires_at=datetime.now() + timedelta(seconds=HANDOVER_TTL),
        )
    )
    user.last_login_at = datetime.now()
    db.commit()

    _write_audit(db, action="SSO登录", user=user, request=request, status_code=200,
           detail=f"{p.name} / {profile.get('username')} (subject={profile.get('subject')})")

    resp = RedirectResponse(url=f"{_base(request)}/?sso=ok", status_code=302)
    secure = _base(request).startswith("https://")
    resp.set_cookie(
        HANDOVER_COOKIE, nonce,
        max_age=HANDOVER_TTL, httponly=True, samesite="lax", path="/", secure=secure,
    )
    # token 用不上（已在会话表里），这里刻意不写任何凭据到浏览器其余位置
    del token
    return resp


@router.get("/handover", response_model=dict)
def handover(request: Request, db: Session = Depends(get_db)):
    """用手里的 handover cookie 换正式 token。一次性，用完即焚。

    前端冷启动时无条件调一次：没有 cookie 就返回 {"ok": false}，
    页面照常显示账密登录框 —— 所以对纯本地部署是零影响。
    """
    nonce = request.cookies.get(HANDOVER_COOKIE)
    if not nonce:
        return {"ok": False}
    row = db.get(m.SsoLoginState, nonce)
    if not row or row.stage != "ready" or row.used or row.expires_at < datetime.now():
        return {"ok": False, "reason": "登录凭证已失效，请重新发起单点登录"}
    row.used = True
    db.flush()

    user = None
    if row.username_attempt:
        user = db.query(m.AppUser).filter(m.AppUser.username == row.username_attempt).first()
    if not user:
        return {"ok": False, "reason": "账号已不存在，请联系管理员"}
    if not user.active:
        return {"ok": False, "reason": "账号已停用，请联系管理员"}

    p = db.get(m.SsoProvider, row.provider_id)
    hours = (p.session_hours if p and p.session_hours and p.session_hours > 0 else None)
    token = sec.create_session(db, user, request, hours=hours,
                               sso_provider_id=p.id if p else None)
    user.last_login_at = datetime.now()
    db.commit()

    provider_id = row.provider_id
    db.delete(row)
    db.commit()

    resp = JSONResponse({
        "ok": True,
        "token": token,
        "expires_in": int((hours or sec.TOKEN_TTL_HOURS) * 3600),
        "user": user_out(user),
        "provider_id": provider_id,
    })
    resp.delete_cookie(HANDOVER_COOKIE, path="/")
    return resp


@router.get("/logout")
def sso_logout(
    request: Request,
    mode: str = Query("redirect", pattern="^(redirect|json)$"),
    user: m.AppUser = Depends(sec.current_user),
    token: str | None = Depends(sec.current_token),
    db: Session = Depends(get_db),
):
    """单点登出：吊销本系统会话，并按配置把浏览器送去门户的登出地址。

    门户没配 logout_url 时就是一次普通登出，行为与 /api/auth/logout 一致。
    """
    if token:
        row = db.query(m.UserSession).filter(
            m.UserSession.token_hash == sec._hash_token(token)
        ).first()
        provider_id = row.sso_provider_id if row else None
        if row:
            row.revoked = True
    else:
        provider_id = None
    _write_audit(db, action="SSO登出", user=user, request=request, status_code=200)
    db.commit()

    p = db.get(m.SsoProvider, provider_id) if provider_id else None
    target = None
    if p and (p.logout_url or "").strip():
        target = p.logout_url.strip()
        # OIDC 的标准参数：门户登出后把用户送回本系统登录页
        back = f"{_base(request)}/"
        sep = "&" if "?" in target else "?"
        target = f"{target}{sep}post_logout_redirect_uri={quote(back, safe='')}"
    if mode == "json":
        # 前端要拿地址自己跳（带 Bearer 的请求没法依赖浏览器跟随 302），
        # 所以这里有 JSON 分支；门户没配登出地址时返回 null，前端退回普通登出。
        return JSONResponse({"ok": True, "logout_url": target, "provider": p.name if p else None})
    return RedirectResponse(url=target or f"{_base(request)}/", status_code=302)


# ================================================================ 管理员配置
class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    protocol: str = m.PROTO_OIDC
    enabled: bool = False
    issuer: str | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    userinfo_url: str | None = None
    validate_url: str | None = None
    jwks_url: str | None = None
    logout_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None   # 留空=不改；"KEEP"=保留旧值；掩码下发时也用 KEEP
    scope: str | None = "openid profile email"
    use_pkce: bool = False
    jwt_source: str | None = "query"
    jwt_param: str | None = "token"
    jwt_secret: str | None = None
    jwt_audience: str | None = None
    claim_subject: str = "sub"
    claim_username: str | None = "preferred_username"
    claim_name: str | None = "name"
    claim_employee_no: str | None = None
    claim_department: str | None = None
    claim_email: str | None = None
    claim_phone: str | None = None
    claim_groups: str | None = None
    auto_create: bool = True
    default_role: str = m.ROLE_APPLICANT
    role_map: dict[str, str] | None = None
    sync_profile: bool = True
    allow_local_login: bool = True
    bind_local_by_username: bool = False
    session_hours: int = 12
    verify_ssl: bool = True
    timeout_sec: int = 8
    remark: str | None = None


KEEP = "KEEP"  # 前端把没改的密钥原样回传，避免误把掩码当密钥保存


def _apply_secret(field_name: str, value: str | None, current: str | None) -> str | None:
    """None/''/KEEP 都表示「不动」，只有真的填了才重新加密。"""
    cur = f"{field_name}_enc"
    if value is None or str(value).strip() in ("", KEEP):
        return current
    return crypt.encrypt(str(value))


@admin_router.get("/providers", response_model=dict)
def admin_list(request: Request,
               admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
               db: Session = Depends(get_db)):
    rows = db.query(m.SsoProvider).order_by(m.SsoProvider.id).all()
    base = _base(request)
    return {
        "items": [_provider_dict(r, base=base) for r in rows],
        "protocols": [{"value": k, "label": v} for k, v in m.SSO_PROTOCOL_LABELS.items()],
        "roles": m.ROLES,
    }


@admin_router.post("/providers", response_model=dict, status_code=201)
def admin_create(payload: ProviderIn, request: Request,
                 admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
                 db: Session = Depends(get_db)):
    if payload.protocol not in m.SSO_PROTOCOLS:
        raise HTTPException(400, f"协议必须是 {'/'.join(m.SSO_PROTOCOLS)} 之一")
    if payload.default_role not in m.ROLES:
        raise HTTPException(400, f"默认角色必须是 {'/'.join(m.ROLES)} 之一")
    if db.query(m.SsoProvider).filter(m.SsoProvider.name == payload.name.strip()).first():
        raise HTTPException(400, "同名身份源已存在")

    data = payload.model_dump()
    client_secret = data.pop("client_secret", None)
    jwt_secret = data.pop("jwt_secret", None)
    role_map = data.pop("role_map", None) or {}
    obj = m.SsoProvider(**{k: v for k, v in data.items() if hasattr(m.SsoProvider, k)})
    obj.role_map = json.dumps(role_map, ensure_ascii=False)
    obj.client_secret_enc = _apply_secret("client_secret", client_secret, None)
    obj.jwt_secret_enc = _apply_secret("jwt_secret", jwt_secret, None)
    db.add(obj)
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        raise HTTPException(400, f"保存失败：{e}") from e
    _write_audit(db, action="新增SSO身份源", user=admin, request=request,
           entity="sso_provider", entity_id=obj.id, detail=f"{obj.name} / {obj.protocol}")
    return _provider_dict(obj, base=_base(request))


@admin_router.get("/providers/{pid}", response_model=dict)
def admin_get(pid: int, request: Request,
              admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
              db: Session = Depends(get_db)):
    return _provider_dict(_get_provider(db, pid), base=_base(request))


@admin_router.put("/providers/{pid}", response_model=dict)
def admin_update(pid: int, payload: ProviderIn, request: Request,
                 admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
                 db: Session = Depends(get_db)):
    obj = _get_provider(db, pid)
    if payload.protocol not in m.SSO_PROTOCOLS:
        raise HTTPException(400, f"协议必须是 {'/'.join(m.SSO_PROTOCOLS)} 之一")
    if payload.default_role not in m.ROLES:
        raise HTTPException(400, f"默认角色必须是 {'/'.join(m.ROLES)} 之一")

    data = payload.model_dump()
    client_secret = data.pop("client_secret", None)
    jwt_secret = data.pop("jwt_secret", None)
    role_map = data.pop("role_map", None)
    changes = []
    for key, value in data.items():
        if key in ("id", "created_at", "updated_at") or not hasattr(obj, key):
            continue
        # None = 这次没提这个字段，保持原值（PUT 全量分片上线时可以少填，不至于被清空）。
        # 要清空某个 URL / claim，显式传空字符串。
        if value is None:
            continue
        old = getattr(obj, key)
        new = value.strip() if isinstance(value, str) else value
        if key in ("issuer", "authorize_url", "token_url", "userinfo_url", "validate_url",
                   "jwks_url", "logout_url", "client_id", "scope", "jwt_param",
                   "jwt_audience", "claim_subject"):
            new = "" if new is None else str(new).strip()
        if old != new:
            changes.append(f"{key}: {old} -> {new}")
            setattr(obj, key, new)
    if client_secret is not None and str(client_secret).strip() not in ("", KEEP):
        obj.client_secret_enc = crypt.encrypt(str(client_secret))
        changes.append("client_secret: 已更新")
    if jwt_secret is not None and str(jwt_secret).strip() not in ("", KEEP):
        obj.jwt_secret_enc = crypt.encrypt(str(jwt_secret))
        changes.append("jwt_secret: 已更新")
    if role_map is not None:
        obj.role_map = json.dumps(role_map, ensure_ascii=False)
        changes.append("role_map: 已更新")

    try:
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        raise HTTPException(400, f"保存失败：{e}") from e
    _write_audit(db, action="修改SSO身份源", user=admin, request=request,
           entity="sso_provider", entity_id=obj.id, detail="; ".join(changes) or "无变化")
    return _provider_dict(obj, base=_base(request))


@admin_router.delete("/providers/{pid}")
def admin_delete(pid: int, request: Request,
                 admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
                 db: Session = Depends(get_db)):
    obj = _get_provider(db, pid)
    bound = db.query(m.AppUser).filter(m.AppUser.sso_provider_id == obj.id).count()
    db.query(m.SsoLoginState).filter(m.SsoLoginState.provider_id == obj.id).delete(
        synchronize_session=False
    )
    db.delete(obj)
    db.commit()
    _write_audit(db, action="删除SSO身份源", user=admin, request=request,
           entity="sso_provider", entity_id=pid,
           detail=f"{obj.name}（已关联账号 {bound} 个，解绑后需自行处置）")
    return {"ok": True, "bound_users": bound}


@admin_router.post("/providers/{pid}/toggle", response_model=dict)
def admin_toggle(pid: int, enabled: bool = Query(...), request: Request = None,
                 admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
                 db: Session = Depends(get_db)):
    obj = _get_provider(db, pid)
    obj.enabled = enabled
    db.commit()
    _write_audit(db, action="启停SSO身份源", user=admin, request=request,
           entity="sso_provider", entity_id=obj.id,
           detail=f"{obj.name} -> {'启用' if enabled else '停用'}")
    return _provider_dict(obj, base=_base(request))


@admin_router.post("/providers/{pid}/test", response_model=dict)
def admin_test(pid: int, request: Request,
               admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
               db: Session = Depends(get_db)):
    """连通性自检：端点通不通、密钥公钥有没有配。不做真实登录（没有用户身份可调）。"""
    obj = _get_provider(db, pid)
    result = sso.test_connection(obj)
    _write_audit(db, action="SSO连通性测试", user=admin, request=request,
           entity="sso_provider", entity_id=obj.id,
           detail=("通过" if result["ok"] else "失败：" + "; ".join(result["notes"])[:200]))
    return result


class PreviewIn(BaseModel):
    raw: dict = Field(default_factory=dict)


@admin_router.post("/providers/{pid}/map-preview", response_model=dict)
def admin_map_preview(pid: int, payload: PreviewIn,
                      admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
                      db: Session = Depends(get_db)):
    """拿一段门户真实响应做字段映射预览。

    管理员最常卡在这一步：门户返回的字段名和文档不一样，配完不知道对不对。
    给个「贴 raw JSON → 看映射结果」的口子，比让他数一遍失败登录强。
    """
    obj = _get_provider(db, pid)
    try:
        profile = sso.normalize_profile(obj, payload.raw)
    except sso.SsoError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "profile": profile,
        "role": sso.mapped_role(obj, profile),
        "role_candidates": sso.role_map(obj),
        "error": None,
    }


class DiscoverIn(BaseModel):
    issuer: str
    verify_ssl: bool = True
    timeout_sec: int = 8


@admin_router.post("/discover", response_model=dict)
def admin_discover(payload: DiscoverIn,
                   admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN))):
    """OIDC 自动发现：给 issuer 回填四个端点，省得照着文档手打。"""
    try:
        info = sso.discover(
            issuer=payload.issuer, timeout=payload.timeout_sec or 8,
            verify_ssl=payload.verify_ssl,
        )
    except sso.SsoError as e:
        detail = dict(sso.last_error())
        raise HTTPException(
            400, detail={"message": str(e), "code": detail.get("code"),
                         "http_status": detail.get("http_status")}
        ) from e
    return info
