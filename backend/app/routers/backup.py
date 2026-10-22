"""数据备份与还原接口（admin 专用）。

  GET    /api/admin/backup/status           存储实况（含宿主路径）、最近一次结果、配置
  GET    /api/admin/backup/list             列出可用的备份包
  POST   /api/admin/backup/run              立即触发一次备份
  POST   /api/admin/backup/ticket           换一张一次性下载票据（给浏览器原生下载用）
  GET    /api/admin/backup/download/{name}  下载指定备份（支持 Range 续传）
  POST   /api/admin/backup/restore          用名字从本地/外挂还原
  POST   /api/admin/backup/upload-restore   上传一份备份包再还原
  POST   /api/admin/backup/prune            按当前保留份数裁剪
  PUT    /api/admin/backup/config/{key}     改配置（仅白名单内）
  PUT    /api/admin/backup/config           批量改配置

下载为什么要有票据（v2.9.28）：
  浏览器原生下载（<a href>）带不上 Authorization 头，而管理员界面用的是 Bearer token，
  于是前端只能 fetch 成 Blob 再触发保存——几个 GB 的备份包会直接把标签页内存撑爆，
  遇到反向代理还常常超时，表现为「点击下载报 HTTP 错误」。
  票据是短期（10 分钟）签名串，只绑定「哪个备份包 + 哪个管理员」，随 URL 走，
  浏览器原生下载即可，服务端也不用把整包读进内存。
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timedelta
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

# ---- 一次性下载票据 ----
# 进程内随机密钥：重启后旧票据自然失效（票据只有 10 分钟，够用）。
_TICKET_SECRET = secrets.token_hex(32)
_TICKET_TTL_SEC = 600


def _ticket_issue(user_id: int, name: str) -> tuple[str, int]:
    exp = int(time.time()) + _TICKET_TTL_SEC
    sig = hmac.new(_TICKET_SECRET.encode(), f"{user_id}:{name}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{exp}.{sig}", _TICKET_TTL_SEC


def _ticket_ok(ticket: str, name: str) -> bool:
    try:
        uid_s, exp_s, sig = (ticket or "").split(".", 2)
        uid, exp = int(uid_s), int(exp_s)
    except Exception:  # noqa: BLE001
        return False
    if exp < time.time():
        return False
    want = hmac.new(_TICKET_SECRET.encode(), f"{uid}:{name}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(want, sig)


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


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _health(last: dict, interval: float) -> dict:
    """备份健康度：给管理员一句人话，而不是让他自己对比时间戳。"""
    if not last:
        return {"level": "warn", "text": "还没有任何备份记录，建议立刻做一次"}
    if last.get("ok") is False or last.get("error"):
        return {"level": "bad", "text": f"最近一次备份失败：{last.get('error') or '未知错误'}"}
    finished = _parse_dt(last.get("finished_at") or "")
    if finished is None:
        return {"level": "ok", "text": "已有备份"}
    age_h = (datetime.now() - finished).total_seconds() / 3600.0
    if interval > 0 and age_h > max(2 * interval, interval + 6):
        return {"level": "warn", "text": f"已 {age_h:.0f} 小时没有新备份（周期设定 {interval:g} 小时）"}
    return {"level": "ok", "text": "备份正常"}


@router.get("/status", response_model=dict)
def backup_status(
    refresh: int = 0,
    _admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """配置 + 存储实况（含宿主路径）+ 最近一次结果 + 健康度。

    refresh=1 时强制重查一次容器挂载表（改完 compose 重新挂了目录时用）。
    """
    cfg = _resolved(db)
    last = _last_result(db)
    storage = bk.storage_view(db, force=bool(refresh))
    items = bk.list_backups(db, which="both")

    next_run_at = None
    finished = _parse_dt((last or {}).get("finished_at") or "")
    if finished is not None and float(cfg["backup_interval_hours"] or 0) > 0:
        next_run_at = (finished + timedelta(hours=float(cfg["backup_interval_hours"]))).isoformat(timespec="minutes")

    counts = {
        "local": sum(1 for i in items if i["where"] == "local"),
        "remote": sum(1 for i in items if i["where"] == "remote"),
    }
    try:
        from .. import docker_api
        docker_ok, docker_msg = docker_api.available()
    except Exception as exc:  # noqa: BLE001
        docker_ok, docker_msg = False, f"{type(exc).__name__}: {exc}"

    return {
        "config": cfg,
        "config_keys": CONFIG_KEYS,
        "last_result": last,
        "storage": storage,
        "counts": counts,
        "next_run_at": next_run_at,
        "health": _health(last, float(cfg["backup_interval_hours"] or 0)),
        "docker": {"available": bool(docker_ok), "message": docker_msg if not docker_ok else ""},
        "copy_command": bk.copy_command(cfg["backup_dir"]),
        "copy_command_remote": bk.copy_command(cfg["backup_remote_dir"]) if cfg.get("backup_remote_dir") else "",
    }


class TicketBody(BaseModel):
    name: str


@router.post("/ticket", response_model=dict)
def backup_ticket(
    payload: TicketBody,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
):
    """换一张一次性下载票据（10 分钟有效，只针对这一份备份包）。"""
    name = payload.name or ""
    if "/" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    ticket, ttl = _ticket_issue(admin.id, name)
    return {
        "ok": True,
        "name": name,
        "ticket": ticket,
        "url": f"/api/admin/backup/download/{name}?ticket={ticket}",
        "expires_in": ttl,
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


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


@router.get("/download/{name}")
def backup_download(
    name: str,
    request: Request,
    db: Session = Depends(get_db),
    ticket: str = "",
):
    """下载备份文件。

    两条认证路径：Authorization: Bearer（前端 fetch 用）或一次性票据 ?ticket=
    （浏览器原生下载用）。支持 Range 续传，且**不再为了填响应头去算整包 sha256**——
    摘要改从备份时落下的 .sha256 读，没有就不给这个头。
    """
    if "/" in name or ".." in name:
        raise HTTPException(400, "非法文件名")

    # --- 认证：Bearer 优先，失败再看票据 ---
    user = None
    auth_err = None
    try:
        user = sec.current_user(request, db)
    except HTTPException as exc:
        auth_err = exc
        user = None
    if user is not None:
        if user.role != m.ROLE_ADMIN:
            raise HTTPException(403, "仅管理员可下载备份包")
    else:
        if not ticket or not _ticket_ok(ticket, name):
            raise HTTPException(401, (auth_err.detail if auth_err else "未登录") + "（请用界面里的下载按钮，票据有效期 10 分钟）")

    bd, rd, *_ = bk._resolved_paths(db)
    candidates: list[Path] = []
    if bd.exists():
        candidates.append(bd / name)
    if rd and rd.exists():
        candidates.append(rd / name)
    src = next((p for p in candidates if p.exists()), None)
    if not src:
        raise HTTPException(404, f"备份包不存在：{name}（可在备份列表里点「刷新」确认）")
    try:
        file_size = src.stat().st_size
    except OSError as exc:
        raise HTTPException(500, f"读取备份包失败：{exc}")

    # 中文文件名用 RFC 5987
    from urllib.parse import quote
    disp = f"attachment; filename=\"reimburse_backup.tar.gz\"; filename*=UTF-8''{quote(name)}"
    headers = {
        "Content-Disposition": disp,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "X-Backup-Size": str(file_size),
    }
    sha = bk.sha256_cached(src)
    if sha:
        headers["X-Backup-SHA256"] = sha

    # --- Range 续传 ---
    start, end = 0, file_size - 1
    status_code = 200
    rng = request.headers.get("range") or request.headers.get("Range")
    if rng:
        mobj = _RANGE_RE.match(rng.strip())
        if not mobj:
            raise HTTPException(416, " Range 头格式错误")
        s_s, e_s = mobj.group(1), mobj.group(2)
        if s_s == "" and e_s == "":
            raise HTTPException(416, "Range 头格式错误")
        if s_s == "":            # bytes=-N：最后 N 字节
            start = max(0, file_size - int(e_s))
        else:
            start = int(s_s)
            end = int(e_s) if e_s else file_size - 1
        if start >= file_size or start > end:
            return StreamingResponse(iter([]), status_code=416, media_type="application/gzip",
                                     headers={**headers, "Content-Range": f"bytes */{file_size}"})
        end = min(end, file_size - 1)
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    length = end - start + 1
    headers["Content-Length"] = str(length)

    def _iter(p: Path, offset: int, remaining: int):
        with open(p, "rb") as f:
            f.seek(offset)
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _iter(src, start, length),
        status_code=status_code,
        media_type="application/gzip",
        headers=headers,
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


class ConfigBatchBody(BaseModel):
    values: dict[str, str]


def _validate_config(key: str, raw: str) -> str:
    """配置项校验：白名单 + 类型。返回规范化后的字符串，不合法直接抛 400。"""
    if key not in CONFIG_KEYS:
        raise HTTPException(400, f"未知配置项：{key}")
    v = (raw or "").strip()
    if key in ("backup_keep_local", "backup_keep_remote"):
        if not v.isdigit() or int(v) < 0:
            raise HTTPException(400, f"{CONFIG_KEYS[key]}：必须是非负整数")
    elif key == "backup_interval_hours":
        try:
            fv = float(v)  # 允许 "0.5" 等小数
        except ValueError:
            raise HTTPException(400, f"{CONFIG_KEYS[key]}：必须是数字")
        if fv < 0:
            raise HTTPException(400, "自动备份周期不能为负")
    elif key == "backup_at_boot":
        if v not in ("0", "1"):
            raise HTTPException(400, "容器启动即备份：只能是 0 或 1")
    elif key in ("backup_dir", "backup_remote_dir"):
        if v and not v.startswith("/"):
            raise HTTPException(400, f"{CONFIG_KEYS[key]}：必须填容器内绝对路径（以 / 开头）")
    return v


def _save_config(db: Session, admin: m.AppUser, request: Request, key: str, raw: str) -> str:
    v = _validate_config(key, raw)
    row = db.get(m.Setting, key)
    if row is None:
        db.add(m.Setting(key=key, value=v, remark=CONFIG_KEYS[key]))
    else:
        row.value = v
        row.remark = CONFIG_KEYS[key]
    db.commit()
    _audit(db, request, admin, "backup_config_set", f"{key}={v}")
    return v


@router.put("/config/{key}", response_model=dict)
def backup_set_config(
    key: str,
    payload: ConfigBody,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    v = _save_config(db, admin, request, key, payload.value)
    return {"ok": True, "key": key, "value": v}


@router.put("/config", response_model=dict)
def backup_set_config_batch(
    payload: ConfigBatchBody,
    request: Request,
    admin: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """一次保存多项配置（界面改完点「保存策略」时用它，避免逐项闪 toast）。"""
    saved: dict[str, str] = {}
    for key, raw in (payload.values or {}).items():
        saved[key] = _save_config(db, admin, request, key, str(raw))
    return {"ok": True, "saved": saved}


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