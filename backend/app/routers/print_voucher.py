"""凭证打印数据接口（v2.9.29）

    GET /api/print/vouchers?ids=1,2,3          按发票 id 取打印清单
    GET /api/print/vouchers?reimbursement_id=8 取整张报销单的打印清单

为什么不在前端逐个调 /api/invoices/{id}/attachments：
  一张报销单动辄十几张票，逐票请求会打出十几个并发，且前端拿不到统一的
  「哪张票没有影像」统计。这里一次查齐，顺带把打印顺序定好——
  发票本体在前、随票凭证（行程单/水单/其他）在后，跟贴票顺序一致。

鉴权：只要登录就能调，但发票要过 invoice_scope_conds 的可见范围过滤；
      传 reimbursement_id 时额外过一次报销单可见性，避免越权拼 id 拿别人的票。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models as m
from .. import security as sec
from .. import serializers as ser
from ..database import get_db

router = APIRouter(prefix="/api/print", tags=["打印"])

# 随票凭证的打印排序：发票本体 → 行程单 → 消费水单 → 其他，同类型内按 id
_KIND_ORDER = {
    m.DOC_INVOICE: 0,
    m.DOC_ITINERARY: 1,
    m.DOC_FOLIO: 2,
    m.DOC_OTHER: 3,
}

_IMAGE_MIME = ("image/",)
_PDF_MIME = ("application/pdf",)


def _doc_out(a: m.InvoiceAttachment) -> dict:
    kind = getattr(a, "kind", None) or m.DOC_INVOICE
    mime = (a.mime or "").split(";")[0].strip().lower()
    ext = (Path(a.filename or a.stored_name or "").suffix or "").lower()
    # 元数据不可信时按后缀兜底：邮件收票、手机上传常见 mime 为空或
    # application/octet-stream，只认 mime 会把能打的图片/PDF 误判成「原件」，
    # 打印出来就是一张占位卡（用户看到的「没有票面内容」）。
    if not mime or mime == "application/octet-stream":
        if ext in (".pdf",):
            mime = "application/pdf"
        elif ext in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif ext == ".png":
            mime = "image/png"
        elif ext == ".webp":
            mime = "image/webp"
        elif ext == ".ofd":
            mime = "application/ofd"
    if mime in _PDF_MIME:
        media = "pdf"
    elif mime.startswith(_IMAGE_MIME):
        media = "image"
    elif ext == ".ofd" or mime == "application/ofd":
        # OFD 电子发票原件：服务端转 SVG（/attachments/{id}/ofd.svg），可预览可打印
        media = "ofd"
    else:
        # xml/xlsx 等原件没法直接上图，打印时给占位卡
        media = "other"
    return {
        "id": a.id,
        "kind": kind,
        "kind_label": m.DOC_KIND_LABELS.get(kind, kind),
        "filename": a.filename,
        "ext": ext.lstrip("."),
        "mime": mime,
        "media": media,
        "size": a.size,
        "url": f"/api/attachments/{a.id}/raw",
        **({"svg_url": f"/api/attachments/{a.id}/ofd.svg"} if media == "ofd" else {}),
    }


def _invoice_out(v: m.Invoice) -> dict:
    docs = sorted(
        (v.attachments or []),
        key=lambda a: (_KIND_ORDER.get(getattr(a, "kind", None) or m.DOC_INVOICE, 9), a.id),
    )
    return {
        "id": v.id,
        "invoice_no": v.invoice_no,
        "invoice_code": v.invoice_code,
        "invoice_type": v.invoice_type,
        "invoice_date": v.invoice_date.isoformat() if v.invoice_date else None,
        "amount": float(v.amount or 0),
        "seller_name": v.seller_name,
        "category_name": v.category.name if v.category else None,
        "reimbursement_code": v.reimbursement.code if v.reimbursement else None,
        "reimbursement_id": v.reimbursement_id,
        "attachments": [_doc_out(a) for a in docs],
    }


@router.get("/reimbursements", response_model=dict)
def print_reimbursements(
    user: Annotated[m.AppUser, Depends(sec.current_user)],
    db: Session = Depends(get_db),
    ids: str = Query("", description="报销单 id，英文逗号分隔"),
):
    """批量打印：一次取多张报销单的完整打印数据（明细 / 关联发票 / 流转记录）。

    跟 /vouchers 一样放在打印接口里而不是让前端逐单调 /api/reimbursements/{id}：
    勾二十张单就是二十个并发请求，中途有一张失败还得分批重试。

    越权的单**跳过而不是报错**——勾选里混进一张看不了的单，不该让整批都打
    不出来，但少了几张必须如实告诉用户（skipped 里带原因）。
    """
    id_list: list[int] = []
    for part in ids.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise HTTPException(400, f"报销单 id 不是数字：{part}")
        id_list.append(int(part))
    if not id_list:
        raise HTTPException(400, "请选择要打印的报销单")
    if len(id_list) > 100:
        raise HTTPException(400, "一次最多打印 100 张报销单，请分批处理")

    rows = (
        db.query(m.Reimbursement)
        .filter(m.Reimbursement.id.in_(id_list))
        .all()
    )
    by_id = {r.id: r for r in rows}
    items: list[dict] = []
    skipped: list[dict] = []
    for oid in id_list:
        r = by_id.get(oid)
        if not r:
            skipped.append({"id": oid, "reason": "单据不存在或已删除"})
            continue
        if not sec.can_view(user, r):
            skipped.append({"id": oid, "reason": "无权查看该单据"})
            continue
        items.append(ser.reimbursement_detail(r))

    return {
        "items": items,
        "stats": {
            "count": len(items),
            "total_amount": round(
                sum(float(i.get("total_amount") or 0) for i in items), 2
            ),
            "skipped": skipped,
            "skipped_count": len(skipped),
        },
    }


@router.get("/vouchers", response_model=dict)
def print_vouchers(
    user: Annotated[m.AppUser, Depends(sec.current_user)],
    db: Session = Depends(get_db),
    ids: str = Query("", description="发票 id，英文逗号分隔"),
    reimbursement_id: int = Query(0, description="报销单 id（与 ids 二选一）"),
):
    rid = reimbursement_id or 0
    id_list: list[int] = []
    if ids.strip():
        for part in ids.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise HTTPException(400, f"发票 id 不是数字：{part}")
            id_list.append(int(part))

    if not rid and not id_list:
        raise HTTPException(400, "请提供 ids 或 reimbursement_id")

    conds = sec.invoice_scope_conds(user)
    q = db.query(m.Invoice)

    reb = None
    if rid:
        reb = db.get(m.Reimbursement, rid)
        if not reb:
            raise HTTPException(404, "报销单不存在")
        # 可见性：管理员/财务放行，其余走 can_view（越权直接 403，不静默返回空）
        if user.role not in (m.ROLE_ADMIN, m.ROLE_FINANCE):
            if not sec.can_view(user, reb):
                raise HTTPException(403, "无权访问该报销单")
        q = q.filter(m.Invoice.reimbursement_id == rid)
    else:
        q = q.filter(m.Invoice.id.in_(id_list))

    for c in conds:
        q = q.filter(c)

    invoices = sorted(q.all(), key=lambda v: v.id)
    # 按 ids 传入的顺序不好还原时，至少保证同一报销单内按 id 升序，跟明细顺序一致

    out = [_invoice_out(v) for v in invoices]

    doc_count = sum(len(i["attachments"]) for i in out)
    pdf_count = sum(1 for i in out for a in i["attachments"] if a["media"] == "pdf")
    image_count = sum(1 for i in out for a in i["attachments"] if a["media"] == "image")
    other_count = doc_count - pdf_count - image_count

    return {
        "reimbursement": (
            {
                "id": reb.id,
                "code": reb.code,
                "title": reb.title,
                "applicant": reb.applicant.name if reb.applicant else None,
                "department": reb.department.name if reb.department else None,
                "total": float(reb.total_amount or 0),
                "status": reb.status,
            }
            if reb
            else None
        ),
        "invoices": out,
        "stats": {
            "invoice_count": len(out),
            "doc_count": doc_count,
            "pdf_count": pdf_count,
            "image_count": image_count,
            "other_count": other_count,
            "no_doc_invoices": sum(1 for i in out if not i["attachments"]),
            "missing_invoices": (
                len(id_list) - len(out) if id_list and not rid else 0
            ),
        },
    }
