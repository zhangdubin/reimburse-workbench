"""Jev AI 决策网关配置（v2.7.10+）

admin 通过前端管理 Jev API key / base_url / timeout，无需重启服务。
复用现有 setting 表（key-value），但走专用端点避免其他角色误读。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import jev_client
from .. import models as m
from .. import security as sec
from ..database import get_db

router = APIRouter(prefix="/api/admin/jev-config", tags=["管理"])

# 允许的运行时配置 key（白名单，避免任意 key 写入 setting 表）
# 用**元组**而非 set：前端按这个顺序渲染表单行，用 set 的话行序会随机变化
_ALLOWED_KEYS = ("jev_key", "jev_base_url", "jev_timeout_sec")
_MASK = "***"

# 描述（前端表单用）
_DESCRIPTIONS = {
    "jev_key": "Vercel AI Gateway 的 key（形如 vck_…，控制台 AI Gateway → API Keys 创建）",
    "jev_base_url": "网关根地址（默认 https://ai-gateway.vercel.sh；调用时会自动补 /v1/evaluate）",
    "jev_timeout_sec": "单次调用超时（秒）",
}


def _mask(key: str, value: str | None) -> str:
    """API key 在 GET 返回时打码。"""
    if key == "jev_key" and value:
        if len(value) <= 8:
            return "***"
        return value[:4] + "***" + value[-4:]
    return value or ""


@router.get("", response_model=dict)
def list_jev_config(
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """返回 Jev 三个配置项；API key 打码显示（不可见明文）。"""
    rows = db.query(m.Setting).filter(m.Setting.key.in_(_ALLOWED_KEYS)).all()
    items = []
    for k in _ALLOWED_KEYS:
        row = next((r for r in rows if r.key == k), None)
        items.append({
            "key": k,
            "value": _mask(k, row.value if row else None),
            "is_set": bool(row and row.value),
            "description": _DESCRIPTIONS.get(k, ""),
            "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds") if row and row.updated_at else None,
        })
    return {
        "items": items,
        "available": jev_client.jev_available(db),
    }


class ConfigItem(BaseModel):
    value: str


@router.put("/{key}", response_model=dict)
def update_jev_config(
    key: str,
    payload: ConfigItem,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """更新某个 Jev 配置项；写入后立即失效内存缓存（5s 内生效）。"""
    if key not in _ALLOWED_KEYS:
        raise HTTPException(400, f"未知配置项 {key}")
    val = (payload.value or "").strip()
    if key == "jev_timeout_sec":
        try:
            float(val)
        except ValueError:
            raise HTTPException(400, "timeout 必须是数字（秒）")
    row = db.get(m.Setting, key)
    if row is None:
        row = m.Setting(key=key, value=val, remark=_DESCRIPTIONS.get(key, ""))
        db.add(row)
    else:
        row.value = val
    db.commit()
    jev_client.invalidate_jev_config_cache()
    # 审计
    try:
        db.add(m.AuditLog(
            user_id=admin.id, action="update", resource_type="setting",
            resource_id=key, ip=request.client.host if request.client else None,
            detail=f"jev 配置更新（脱敏）",
        ))
        db.commit()
    except Exception:
        db.rollback()
    return {"status": "ok", "key": key}


@router.delete("/{key}", response_model=dict)
def delete_jev_config(
    key: str,
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """删除某个配置项（恢复 env fallback 或视为未配置）。"""
    if key not in _ALLOWED_KEYS:
        raise HTTPException(400, f"未知配置项 {key}")
    row = db.get(m.Setting, key)
    if row is None:
        return {"status": "ok", "key": key}
    db.delete(row)
    db.commit()
    jev_client.invalidate_jev_config_cache()
    return {"status": "deleted", "key": key}


@router.post("/test", response_model=dict)
def test_jev_config(
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """连通性测试：用保存的 key 发一次最小决策。

    这里**不抛 5xx**：测试结果本身就是返回值。失败时把网关状态码、服务端原文、
    规范化后的端点、耗时一并带上，让管理员一眼看出是没绑卡、key 失效还是路径变了。
    （早期版本抛一句笼统的 502 且不留日志，等于把排查成本转嫁给使用者。）
    """
    result = jev_client.jev_probe(db)
    try:
        db.add(m.AuditLog(
            user_id=admin.id, action="test", resource_type="setting",
            resource_id="jev_key",
            detail=("jev 连通性测试通过" if result.get("ok")
                    else f"jev 连通性测试失败：{str(result.get('error', ''))[:180]}"),
        ))
        db.commit()
    except Exception:
        db.rollback()
    return result