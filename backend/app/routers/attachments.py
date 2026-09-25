"""发票影像上传与下载。

安全要点：
- 磁盘文件名一律自己生成（随机 hex + 白名单后缀），绝不使用用户提交的文件名，
  否则 `../../etc/passwd` 这类路径穿越会直接落地。
- 后缀与 Content-Type 双向白名单校验；允许 PDF、图片，以及 ofd/xml/xlsx 电子发票原件。
- 下载响应带 `X-Content-Type-Options: nosniff`，避免浏览器把图片当脚本执行。
- 单文件大小可配，默认 10MB。

白名单、大小上限、落盘与清理统一由 app/filestore.py 提供，
人工上传与邮箱收票共用一份实现，避免两边规则漂移。
"""

from __future__ import annotations

import mimetypes  # noqa: F401  （保留给未来的类型嗅探）
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import filestore as fs
from .. import models as m
from .. import security as sec
from ..database import get_db

router = APIRouter(prefix="/api", tags=["发票影像"])

UPLOAD_DIR = fs.UPLOAD_DIR
MAX_MB = fs.MAX_MB
MAX_BYTES = fs.MAX_BYTES
ALLOWED = fs.ALLOWED
INLINE_EXT = fs.INLINE_EXT

# 能操作发票的角色：财务与管理员
_can_write = sec.require_roles(m.ROLE_FINANCE, m.ROLE_ADMIN)


def attachment_out(a: m.InvoiceAttachment) -> dict:
    return {
        "id": a.id,
        "invoice_id": a.invoice_id,
        "filename": a.filename,
        "size": a.size,
        "mime": a.mime,
        "uploaded_by": a.uploaded_by,
        "created_at": a.created_at.isoformat(sep=" ", timespec="seconds") if a.created_at else None,
        "url": f"/api/attachments/{a.id}/raw",
    }


@router.get("/invoices/{oid}/attachments", response_model=list[dict])
def list_attachments(
    oid: int,
    user: Annotated[m.AppUser, Depends(sec.current_user)],
    db: Session = Depends(get_db),
):
    if not db.get(m.Invoice, oid):
        raise HTTPException(404, "发票不存在")
    rows = (
        db.query(m.InvoiceAttachment)
        .filter(m.InvoiceAttachment.invoice_id == oid)
        .order_by(m.InvoiceAttachment.id.desc())
        .all()
    )
    return [attachment_out(a) for a in rows]


@router.post("/invoices/{oid}/attachments", response_model=dict, status_code=201)
async def upload_attachment(
    oid: int,
    request: Request,
    file: UploadFile = File(...),
    user: m.AppUser = Depends(_can_write),
    db: Session = Depends(get_db),
):
    invoice = db.get(m.Invoice, oid)
    if not invoice:
        raise HTTPException(404, "发票不存在")

    raw_name = (file.filename or "").strip()
    ext = fs.safe_ext(raw_name)
    if ext not in ALLOWED:
        raise HTTPException(400, f"只允许上传 {'/'.join(sorted(ALLOWED))} 格式")
    if file.content_type and file.content_type not in ALLOWED[ext]:
        # 部分浏览器对 jpg 会给 application/octet-stream，这里宽松放行但记下真实类型
        if file.content_type not in ("application/octet-stream", ""):
            raise HTTPException(400, f"文件类型与后缀不匹配：{file.content_type}")

    data = await file.read()
    try:
        saved = fs.save_bytes(raw_name, data, file.content_type)
    except fs.FileRejected as exc:
        raise HTTPException(400, str(exc))

    obj = m.InvoiceAttachment(
        invoice_id=oid,
        filename=saved["filename"],
        stored_name=saved["stored_name"],
        size=saved["size"],
        mime=saved["mime"],
        uploaded_by=user.name,
    )
    db.add(obj)
    # 首次上传时把文件名回填到发票主记录，兼容旧的单附件字段
    if not invoice.file_name:
        invoice.file_name = obj.filename
    db.commit()
    db.refresh(obj)

    db.add(
        m.AuditLog(
            user_id=user.id, username=user.username, role=user.role,
            action="上传发票影像", entity="invoice", entity_id=str(oid),
            method=request.method, path=request.url.path, status_code=201,
            ip=sec._client_ip(request),
            detail=f"{obj.filename} ({saved['size']} bytes) -> 发票 {invoice.invoice_no}",
        )
    )
    db.commit()
    return attachment_out(obj)


@router.get("/attachments/{aid}/raw")
def download_attachment(
    aid: int,
    user: Annotated[m.AppUser, Depends(sec.current_user)],
    db: Session = Depends(get_db),
):
    row = db.get(m.InvoiceAttachment, aid)
    if not row:
        raise HTTPException(404, "附件不存在")
    try:
        path = fs.resolve(row.stored_name)
    except fs.FileRejected:
        raise HTTPException(400, "非法的附件路径")
    if not path.exists():
        raise HTTPException(404, "附件文件已丢失，请重新上传")

    ext = Path(row.stored_name).suffix.lower()
    inline = ext in INLINE_EXT
    return FileResponse(
        path,
        media_type=row.mime or "application/octet-stream",
        filename=row.filename,
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/attachments/{aid}")
def delete_attachment(
    aid: int,
    request: Request,
    user: m.AppUser = Depends(_can_write),
    db: Session = Depends(get_db),
):
    row = db.get(m.InvoiceAttachment, aid)
    if not row:
        raise HTTPException(404, "附件不存在")
    name = row.filename
    invoice_id = row.invoice_id
    stored = row.stored_name
    db.delete(row)
    db.commit()
    fs.delete_stored(stored)
    db.add(
        m.AuditLog(
            user_id=user.id, username=user.username, role=user.role,
            action="删除发票影像", entity="invoice", entity_id=str(invoice_id),
            method=request.method, path=request.url.path, status_code=200,
            ip=sec._client_ip(request), detail=name,
        )
    )
    db.commit()
    return {"ok": True, "deleted": aid}
