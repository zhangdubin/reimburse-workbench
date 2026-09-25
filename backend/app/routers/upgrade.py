"""在线升级接口（v2.9.0+）—— admin 专用

  GET    /api/admin/upgrade/status         当前版本 + 最新版本（缓存）+ 升级进展
  POST   /api/admin/upgrade/check          立即去 GitHub 查一次
  POST   /api/admin/upgrade/apply          一键升级
  GET    /api/admin/upgrade/config         读配置（token 打码）
  PUT    /api/admin/upgrade/config/{key}   改配置
  DELETE /api/admin/upgrade/config/{key}   删配置（回到 env 兜底）

只有管理员能看到和操作：升级等于对宿主 Docker 下发指令，
权限上比普通业务操作高一档。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import docker_api as dk
from .. import models as m
from .. import security as sec
from .. import upgrade_client as uc
from .. import upgrade_runner as ur
from ..database import get_db

router = APIRouter(prefix="/api/admin/upgrade", tags=["管理"])

_LATEST_KEY = "upgrade_latest"     # 最近一次检查结果的缓存（JSON）
_MASK = "***"


def _mask(key: str, value: str) -> str:
    if key == "upgrade_token" and value:
        if len(value) <= 8:
            return _MASK
        return value[:4] + "***" + value[-4:]
    return value or ""


def _read_cached(db) -> dict:
    row = db.get(m.Setting, _LATEST_KEY)
    if row is None or not (row.value or "").strip():
        return {}
    try:
        return json.loads(row.value)
    except Exception:  # noqa: BLE001
        return {}


def _write_cached(db, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    row = db.get(m.Setting, _LATEST_KEY)
    if row is None:
        db.add(m.Setting(key=_LATEST_KEY, value=payload, remark="在线升级：最近一次版本检查结果"))
    else:
        row.value = payload
    db.commit()


def _trim(rel: dict) -> dict:
    """给前端看的版本信息（不要把 manifest 里一堆哈希全塞过去）。"""
    manifest = rel.get("manifest") or {}
    return {
        "version": rel.get("version") or "",
        "tag": rel.get("tag") or "",
        "notes": rel.get("notes") or "",
        "published_at": rel.get("published_at") or "",
        "html_url": rel.get("html_url") or "",
        "prerelease": bool(rel.get("prerelease")),
        "has_update": bool(rel.get("has_update")),
        "manifest_ok": bool(rel.get("manifest")),
        "manifest_error": rel.get("manifest_error") or "",
        "released_at_manifest": manifest.get("released_at") or "",
        "schema_changed": bool(manifest.get("schema_changed")),
        "min_from": manifest.get("min_from") or "",
        "checked_at": rel.get("checked_at") or "",
        "error": rel.get("error") or "",
        "ok": bool(rel.get("ok")),
    }


@router.get("/status", response_model=dict)
def upgrade_status(
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    cfg_ok, cfg_msg = uc.configured(db)
    cfg = uc._cfg(db)
    st = ur.read_status()
    cached = _read_cached(db)
    return {
        "current_version": uc.current_version(),
        "docker_ready": st.get("docker_ready"),
        "docker_hint": st.get("docker_hint"),
        "configured": cfg_ok,
        "configure_message": cfg_msg,
        "repo": (cfg.get("upgrade_repo") or "").strip(),
        "auto_check": uc.auto_check_on(db),
        "interval_hours": uc.interval_hours(db),
        "require_manifest": uc.require_manifest(db),
        "latest": cached,
        "progress": {
            "phase": st.get("phase"),
            "ok": st.get("ok"),
            "message": st.get("message") or "",
            "detail": st.get("detail") or "",
            "progress": st.get("progress") or 0.0,
            "target_version": st.get("target_version") or "",
            "from_version": st.get("from_version") or "",
            "started_at": st.get("started_at") or "",
            "finished_at": st.get("finished_at") or "",
            "operator": st.get("operator") or "",
            "log": st.get("log") or [],
        },
    }


@router.post("/check", response_model=dict)
def upgrade_check(
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    rel = uc.fetch_release(db, timeout=25)
    slim = _trim(rel)
    _write_cached(db, slim)
    try:
        db.add(m.AuditLog(
            user_id=admin.id, action="upgrade_check", resource_type="system",
            resource_id="upgrade", ip=request.client.host if request.client else None,
            detail="检查新版本：%s" % ("有新版本 " + slim["version"] if slim.get("has_update")
                                       else (slim.get("error") or "已是最新")),
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    if not slim.get("ok"):
        raise HTTPException(502, slim.get("error") or "检查失败")
    return slim


class ApplyBody(BaseModel):
    version: str = ""
    confirm: bool = False
    backup: bool = True


@router.post("/apply", response_model=dict)
def upgrade_apply(
    payload: ApplyBody,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """一键升级。下载与校验在本容器做，切换交给独立执行器容器。"""
    if not payload.confirm:
        raise HTTPException(400, "需要显式确认（confirm=true）才会执行升级")
    problems = ur.preflight(db)
    if problems:
        raise HTTPException(409, "；".join(problems))

    result = ur.start_upgrade(db, payload.version, operator=admin.username or "")
    try:
        db.add(m.AuditLog(
            user_id=admin.id, action="upgrade_apply", resource_type="system",
            resource_id=payload.version or "latest",
            ip=request.client.host if request.client else None,
            detail="触发在线升级：%s" % (result.get("target_version") or payload.version or "最新"),
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    if not result.get("ok"):
        raise HTTPException(409, result.get("error") or "无法开始升级")
    return result


@router.get("/config", response_model=dict)
def upgrade_config_list(
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    rows = {r.key: r for r in db.query(m.Setting).filter(m.Setting.key.in_(uc.ALLOWED_KEYS)).all()}
    items = []
    for key in uc.ALLOWED_KEYS:
        row = rows.get(key)
        items.append({
            "key": key,
            "value": _mask(key, row.value if row else None),
            "is_set": bool(row and (row.value or "").strip()),
            "description": uc.DESCRIPTIONS.get(key, ""),
            "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds")
            if row is not None and row.updated_at else None,
        })
    ok, hint = dk.available()
    return {"items": items, "docker_ready": ok, "docker_hint": hint,
            "env_fallback": uc._ENV_FALLBACK}


class ConfigItem(BaseModel):
    value: str


@router.put("/config/{key}", response_model=dict)
def upgrade_config_set(
    key: str,
    payload: ConfigItem,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if key not in uc.ALLOWED_KEYS:
        raise HTTPException(400, "未知配置项 %s" % key)
    val = (payload.value or "").strip()
    if key == "upgrade_repo" and val and "/" not in val:
        raise HTTPException(400, "仓库要写成 owner/repo")
    if key == "upgrade_interval_hours" and val:
        try:
            float(val)
        except ValueError:
            raise HTTPException(400, "间隔必须是数字（小时）")
    row = db.get(m.Setting, key)
    if row is None:
        db.add(m.Setting(key=key, value=val, remark=uc.DESCRIPTIONS.get(key, "")))
    else:
        row.value = val
    db.commit()
    uc.invalidate_upgrade_config_cache()
    try:
        db.add(m.AuditLog(
            user_id=admin.id, action="update", resource_type="setting", resource_id=key,
            ip=request.client.host if request.client else None,
            detail="升级配置更新（脱敏）",
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    return {"status": "ok", "key": key}


@router.delete("/config/{key}", response_model=dict)
def upgrade_config_delete(
    key: str,
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if key not in uc.ALLOWED_KEYS:
        raise HTTPException(400, "未知配置项 %s" % key)
    row = db.get(m.Setting, key)
    if row is not None:
        db.delete(row)
        db.commit()
    uc.invalidate_upgrade_config_cache()
    return {"status": "deleted", "key": key}
