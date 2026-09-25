"""数据备份与还原接口（admin 专用）。

  GET    /api/admin/backup/status           本地/外挂目录、最近一次结果、配置
  GET    /api/admin/backup/list             列出可用的备份包
  POST   /api/admin/backup/run              立即触发一次备份
  GET    /api/admin/backup/download/{name}  下载指定备份（带 sha 校验头）
  POST   /api/admin/backup/restore          用名字从本地/外挂还原
  POST   /api/admin/backup/upload-restore   上传一份备份包再还原
  POST   /api/admin/backup/prune            按当前保留份数裁剪
  PUT    /api/admin/backup/config/{key}     改配置（仅白名单内）
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import backup as bk
from .. import models as m
from .. import security as sec
from ..database import get_db

router = APIRouter(prefix="/api/admin/backup", tags=["管理"])

# 配置白名单 + 类型校验（拒绝塞不认识的 key 进 setting）
CONFIG_KEYS: dict[str, str] = {
    "backup_dir": "本地备份根目录（容器内绝对路径）",
    "backup_remote_dir": "外挂备份目录（空字符串=关闭；典型为宿主/NFS/SMB 挂载点）",
    "backup_keep_local": "本地保留份数（正整数；0=不裁剪）",
    "backup_keep_remote": "外挂保留份数（正整数；0=不裁剪）",
    "backup_interval_hours": "自动备份周期（小时；0=关闭自动）",
    "backup_at_boot": "容器启动时立即做一次（1=是）",
}


def _audit(db: Session, request: Request, user: m.AppUser, action: str, detail: str) -> None:
    try:
        db.add(m.AuditLog(
            user_id=user.id, action=action, resource_type="system",
            resource_id="backup",
            ip=request.client.host if request.client else None,
            detail=detail[:1000],
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _last_result(db: Session) -> dict:
    row = db.get(m.Setting, "backup_last_result")
    if not row or not (row.value or "").strip():
        return {}
    try:
        return json.loads(row.value)
    except Exception:  # noqa: BLE001
        return {}


def _resolved(db: Session) -> dict:
    bd, rd, kl, kr, itv, ab = bk._resolved_paths(db)
    return {
        "backup_dir": str(bd),
        "backup_remote_dir": str(rd) if rd else "",
        "backup_keep_local": kl,
        "backup_keep_remote": kr,
        "backup_interval_hours": itv,
        "backup_at_boot": 1 if ab else 0,
    }


@router.get("/status", response_model=dict)
def backup_status(
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """配置 + 最近一次结果。供前端「数据备份」页读取。"""
    return {
        "config": _resolved(db),
        "config_keys": CONFIG_KEYS,
        "last_result": _last_result(db),
    }


@router.get("/list", response_model=dict)
def backup_list(
    which: str = "both",
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if which not in ("local", "remote", "both"):
        raise HTTPException(400, "which 只能是 local|remote|both")
    items = bk.list_backups(db, which=which)
    return {"items": items, "count": len(items)}


@router.post("/run", response_model=dict)
def backup_run(
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """立即做一次备份（同步返回结果）。大库可能跑 10s-数分钟。"""
    try:
        result = bk.do_backup(db, label=f"manual_{admin.username}")
        prune_info = bk.prune(db)
        _audit(db, request, admin, "backup_run",
               f"手动备份 {result.name}，{result.size // 1024} KB，sha={result.sha256[:12]}")
        return {"ok": True, "result": result.to_dict(), "prune": prune_info}
    except Exception as exc:  # noqa: BLE001
        # 失败也要把信息落 setting，让前端能看到
        try:
            import json as _json, time as _t
            row = db.get(m.Setting, "backup_last_result")
            payload = _json.dumps({
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False)
            if row: row.value = payload
            else: db.add(m.Setting(key="backup_last_result", value=payload, remark="最近一次备份结果（成功/失败/大小/sha）"))
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        _audit(db, request, admin, "backup_run_failed", f"{type(exc).__name__}: {exc}")
        raise HTTPException(500, f"备份失败：{exc}")


@router.get("/download/{name}")
def backup_download(
    name: str,
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """下载备份文件（用流式响应，避免大文件吃内存）。"""
    if "/" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    bd, rd, *_ = bk._resolved_paths(db)
    candidates: list[Path] = []
    if bd.exists():
        candidates.append(bd / name)
    if rd and rd.exists():
        candidates.append(rd / name)
    src = next((p for p in candidates if p.exists()), None)
    if not src:
        raise HTTPException(404, f"备份包不存在：{name}")

    def _iter(p: Path):
        with open(p, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    # 中文文件名用 RFC 5987
    fname_ascii = "reimburse_backup.tar.gz"
    fname_utf8 = name
    disp = f'attachment; filename="{fname_ascii}"; filename*=UTF-8\'\'{fname_utf8}'
    return StreamingResponse(
        _iter(src),
        media_type="application/gzip",
        headers={
            "Content-Disposition": disp,
            "X-Backup-SHA256": bk._sha256_file(src),
            "X-Backup-Size": str(src.stat().st_size),
        },
    )


class RestoreBody(BaseModel):
    source: str           # 文件名 / 绝对路径 / "uploaded"
    confirm: bool = False


@router.post("/restore", response_model=dict)
def backup_restore(
    payload: RestoreBody,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """从已有备份还原。必须 confirm=true。"""
    if not payload.confirm:
        raise HTTPException(400, "需要 confirm=true 才会执行还原")
    res = bk.do_restore(db, source=payload.source)
    if not res.ok:
        _audit(db, request, admin, "backup_restore_failed",
               f"来源={payload.source}，失败：{res.message}")
        raise HTTPException(500, res.message)
    _audit(db, request, admin, "backup_restore",
           f"来源={payload.source}，成功：{res.message}")
    return {"ok": True, "message": res.message, "detail": res.detail}


@router.post("/upload-restore", response_model=dict)
async def backup_upload_restore(
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
    confirm: bool = False,
    file: UploadFile = File(...),
):
    """上传一份备份包并立即还原（用于跨机恢复）。

    confirm 必须为 true（建议从 query 传，少一个 body schema 嵌套）。
    上传包先存到 /tmp/_restore.tar.gz 再走 do_restore。
    """
    if not confirm:
        raise HTTPException(400, "需要 confirm=true 才会执行还原")
    # 临时落盘（带大小保护，避免磁盘爆掉）
    tmp = Path("/tmp/_restore.tar.gz")
    try:
        # 上限 12GB（比 BACKUP_MAX_BYTES 默认 10GB 稍大，给点余量）
        size = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > bk.BACKUP_MAX_BYTES + 2 * 1024 * 1024 * 1024:
                    raise HTTPException(413, "上传文件过大")
                f.write(chunk)
        # sha 顺手记一下，写审计用
        sha = bk._sha256_file(tmp)
        res = bk.do_restore(db, source="uploaded")
        if not res.ok:
            _audit(db, request, admin, "backup_restore_upload_failed",
                   f"上传包 sha={sha[:12]}，失败：{res.message}")
            raise HTTPException(500, res.message)
        _audit(db, request, admin, "backup_restore_upload",
               f"上传包 sha={sha[:12]}，{size // 1024} KB；成功：{res.message}")
        return {"ok": True, "message": res.message, "sha256": sha, "detail": res.detail}
    finally:
        try: tmp.unlink()
        except OSError: pass


@router.post("/prune", response_model=dict)
def backup_prune(
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    info = bk.prune(db)
    _audit(db, request, admin, "backup_prune", json.dumps(info, ensure_ascii=False))
    return info


class ConfigBody(BaseModel):
    value: str


@router.put("/config/{key}", response_model=dict)
def backup_set_config(
    key: str,
    payload: ConfigBody,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if key not in CONFIG_KEYS:
        raise HTTPException(400, f"未知配置项：{key}")
    v = (payload.value or "").strip()
    # 数字类做格式校验，避免脏值把后面读 path 的代码带崩
    if key in ("backup_keep_local", "backup_keep_remote"):
        if not v.isdigit() or int(v) < 0:
            raise HTTPException(400, f"{key} 必须是非负整数")
    elif key == "backup_interval_hours":
        try:
            float(v)  # 允许 "0.5" 等小数
        except ValueError:
            raise HTTPException(400, f"{key} 必须是数字")
        if float(v) < 0:
            raise HTTPException(400, f"{key} 不能为负")
    elif key == "backup_at_boot":
        if v not in ("0", "1"):
            raise HTTPException(400, f"{key} 只能是 0 或 1")
    elif key in ("backup_dir", "backup_remote_dir"):
        if v and not v.startswith("/"):
            raise HTTPException(400, f"{key} 必须是绝对路径")

    row = db.get(m.Setting, key)
    if row is None:
        db.add(m.Setting(key=key, value=v, remark=CONFIG_KEYS[key]))
    else:
        row.value = v
        row.remark = CONFIG_KEYS[key]
    db.commit()
    _audit(db, request, admin, "backup_config_set", f"{key}={v}")
    return {"ok": True, "key": key, "value": v}


@router.delete("/config/{key}", response_model=dict)
def backup_del_config(
    key: str,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if key not in CONFIG_KEYS:
        raise HTTPException(400, f"未知配置项：{key}")
    row = db.get(m.Setting, key)
    if row:
        db.delete(row)
        db.commit()
    _audit(db, request, admin, "backup_config_del", key)
    return {"ok": True}