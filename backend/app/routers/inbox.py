"""发票收件箱：邮箱收票、自动识别、手工导入。

三条入口共用同一条管线（识别 → 落盘 → 建发票 → 记审计）：

1. **IMAP 直连**：`POST /api/mail-accounts/{id}/sync` 拉取邮箱附件；
2. **外部投递**：`POST /api/inbox/ingest` 由 Agent Mail 等外部通道把附件推过来，
   用 `X-Ingest-Token` 或登录态鉴权，无需把邮箱密码交出去；
3. **手工上传**：`POST /api/inbox/import` 单文件识别入账，`POST /api/inbox/recognize` 只识别不落库。

识别不出来号码的票不会丢：会以 `待识别-xxxxxx` 占位入账并标注「需人工复核」，
人工在发票台账里补全即可——收票最忌讳的是"识别失败 = 票据消失"。
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import ai_service
from .. import approvals as ap
from .. import crypt
from .. import filestore as fs
from .. import mailbox
from .. import models as m
from .. import ocr
from .. import recognize as rec
from .. import security as sec
from ..database import get_db

router = APIRouter(prefix="/api", tags=["发票收件箱"])

_admin = sec.require_roles(m.ROLE_ADMIN)
_staff = sec.require_roles(m.ROLE_FINANCE, m.ROLE_ADMIN)

# 外部投递令牌：不配就只允许登录态调用，避免默认打开一个匿名上传口
INGEST_TOKEN = os.getenv("INBOX_INGEST_TOKEN") or ""
# 低于该置信度就标注「需人工复核」
LOW_CONFIDENCE = 0.6
# 识别来源落库字段只有 16 字符，标注时要留出余量
MAX_SOURCE_LEN = 16


def _tag_source(source: str | None, tag: str) -> str:
    """给识别来源加标（如 pdf -> pdf+ai），并保证不超长。

    落库字段是 String(16)，超长会被数据库拒绝或截断，所以这里主动收敛。
    """
    base = (source or "none").strip()
    if tag in base:
        return base[:MAX_SOURCE_LEN]
    room = MAX_SOURCE_LEN - len(tag) - 1
    return f"{base[:room]}+{tag}"


# 给用户看的字段名，用于「仍未识别：……」这类提示
_FIELD_LABELS = {
    "invoice_no": "发票号码",
    "invoice_code": "发票代码",
    "invoice_type": "发票类型",
    "amount": "价税合计",
    "tax_amount": "税额",
    "tax_rate": "税率",
    "invoice_date": "开票日期",
    "seller_name": "销售方",
    "seller_tax_no": "销售方税号",
    "buyer_name": "购买方",
    "buyer_tax_no": "购买方税号",
}



# ------------------------------------------------------------------ 序列化


def account_out(a: m.MailAccount, *, mask: bool = True) -> dict:
    pwd = crypt.decrypt(a.password_enc) if mask else ""
    return {
        "id": a.id,
        "name": a.name,
        "host": a.host,
        "port": a.port,
        "use_ssl": a.use_ssl,
        "username": a.username,
        "password_masked": crypt.mask(pwd) if mask else "",
        "has_password": bool(a.password_enc),
        "folder": a.folder,
        "only_unseen": a.only_unseen,
        "mark_seen": a.mark_seen,
        "since_days": a.since_days,
        "max_per_sync": a.max_per_sync,
        "subject_keywords": a.subject_keywords or "",
        "sender_allow": a.sender_allow or "",
        "active": a.active,
        "last_sync_at": a.last_sync_at.isoformat(sep=" ", timespec="seconds") if a.last_sync_at else None,
        "last_sync_status": a.last_sync_status,
        "last_sync_detail": a.last_sync_detail,
        "imported_total": a.imported_total,
        "remark": a.remark,
    }


def message_out(x: m.MailMessage) -> dict:
    return {
        "id": x.id,
        "account_id": x.account_id,
        "account_name": x.account.name if x.account else None,
        "subject": x.subject,
        "sender": x.sender,
        "sent_at": x.sent_at.isoformat(sep=" ", timespec="seconds") if x.sent_at else None,
        "attachment_count": x.attachment_count,
        "imported_count": x.imported_count,
        "skipped_count": x.skipped_count,
        "status": x.status,
        "detail": x.detail,
        "created_at": x.created_at.isoformat(sep=" ", timespec="seconds") if x.created_at else None,
    }


# ------------------------------------------------------------------ 邮箱账号


@router.get("/mail-accounts", response_model=list[dict], dependencies=[Depends(_staff)])
def list_mail_accounts(db: Session = Depends(get_db)):
    rows = db.query(m.MailAccount).order_by(m.MailAccount.id.asc()).all()
    return [account_out(a) for a in rows]


@router.post("/mail-accounts", response_model=dict, status_code=201, dependencies=[Depends(_admin)])
def create_mail_account(payload: dict, db: Session = Depends(get_db)):
    name = (payload.get("name") or "").strip()
    host = (payload.get("host") or "").strip()
    username = (payload.get("username") or "").strip()
    if not name or not host or not username:
        raise HTTPException(400, "名称、服务器地址、账号均为必填")
    if db.query(m.MailAccount).filter(m.MailAccount.name == name).first():
        raise HTTPException(400, "同名邮箱账号已存在")
    pwd = payload.get("password") or ""
    try:
        enc = crypt.encrypt(pwd) if pwd else ""
    except crypt.SecretMissing as exc:
        raise HTTPException(500, str(exc))
    obj = m.MailAccount(
        name=name,
        host=host,
        port=int(payload.get("port") or (993 if payload.get("use_ssl", True) else 143)),
        use_ssl=bool(payload.get("use_ssl", True)),
        username=username,
        password_enc=enc,
        folder=(payload.get("folder") or "INBOX").strip(),
        only_unseen=bool(payload.get("only_unseen", True)),
        mark_seen=bool(payload.get("mark_seen", True)),
        since_days=int(payload.get("since_days") or 30),
        max_per_sync=int(payload.get("max_per_sync") or 30),
        subject_keywords=payload.get("subject_keywords") or None,
        sender_allow=payload.get("sender_allow") or None,
        active=bool(payload.get("active", True)),
        remark=payload.get("remark") or None,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return account_out(obj)


@router.put("/mail-accounts/{oid}", response_model=dict, dependencies=[Depends(_admin)])
def update_mail_account(oid: int, payload: dict, db: Session = Depends(get_db)):
    obj = db.get(m.MailAccount, oid)
    if not obj:
        raise HTTPException(404, "邮箱账号不存在")
    if "name" in payload and payload["name"]:
        dup = (
            db.query(m.MailAccount)
            .filter(m.MailAccount.name == payload["name"], m.MailAccount.id != oid)
            .first()
        )
        if dup:
            raise HTTPException(400, "同名邮箱账号已存在")
        obj.name = payload["name"].strip()
    for key in ("host", "username", "folder"):
        if key in payload and payload[key] is not None:
            setattr(obj, key, str(payload[key]).strip())
    for key in ("remark", "subject_keywords", "sender_allow"):
        if key in payload:
            obj.__setattr__(key, payload[key] or None)
    for key in ("port", "since_days", "max_per_sync"):
        if key in payload and payload[key] not in (None, ""):
            setattr(obj, key, int(payload[key]))
    for key in ("use_ssl", "only_unseen", "mark_seen", "active"):
        if key in payload and payload[key] is not None:
            setattr(obj, key, bool(payload[key]))
    # 密码留空表示「不修改」，避免前端回显密文再提交把密码覆盖成星号
    if payload.get("password"):
        try:
            obj.password_enc = crypt.encrypt(payload["password"])
        except crypt.SecretMissing as exc:
            raise HTTPException(500, str(exc))
    db.commit()
    db.refresh(obj)
    return account_out(obj)


@router.delete("/mail-accounts/{oid}", dependencies=[Depends(_admin)])
def delete_mail_account(oid: int, db: Session = Depends(get_db)):
    obj = db.get(m.MailAccount, oid)
    if not obj:
        raise HTTPException(404, "邮箱账号不存在")
    # 收件记录保留（审计需要），只把外键置空
    db.query(m.MailMessage).filter(m.MailMessage.account_id == oid).update(
        {m.MailMessage.account_id: None}, synchronize_session=False
    )
    db.delete(obj)
    db.commit()
    return {"ok": True, "deleted": oid}


@router.post("/mail-accounts/{oid}/test", response_model=dict, dependencies=[Depends(_staff)])
def test_mail_account(oid: int, payload: dict | None = None, db: Session = Depends(get_db)):
    obj = db.get(m.MailAccount, oid)
    if not obj:
        raise HTTPException(404, "邮箱账号不存在")
    cfg = _account_cfg(obj)
    # 允许用表单里临时填的密码测试，免去「先保存才能验」的死循环
    if payload and payload.get("password"):
        cfg["password"] = payload["password"]
    try:
        info = mailbox.test_connection(cfg)
    except mailbox.MailboxError as exc:
        return {"ok": False, "message": str(exc), "folders": []}
    return info


# ------------------------------------------------------------------ 收票管线


def _account_cfg(a: m.MailAccount, password: str | None = None) -> dict:
    return {
        "host": a.host,
        "port": a.port,
        "use_ssl": a.use_ssl,
        "username": a.username,
        "password": password if password is not None else crypt.decrypt(a.password_enc),
        "folder": a.folder,
        "only_unseen": a.only_unseen,
        "mark_seen": a.mark_seen,
        "since_days": a.since_days,
        "max_per_sync": a.max_per_sync,
        "subject_keywords": a.subject_keywords,
        "sender_allow": a.sender_allow,
    }


def _placeholder_no() -> str:
    return f"待识别-{secrets.token_hex(3)}"


def _pick(override: dict, key: str, fallback):
    """override 里显式给了就用它（哪怕是空值），没给才回退到识别结果。

    这样人工在预览页把识别错的字段清空，才不会被识别结果又填回去。
    """
    if key in override:
        return override[key]
    return fallback


def store_one(
    db: Session,
    *,
    user: m.AppUser | None,
    filename: str,
    data: bytes,
    subject: str = "",
    body: str = "",
    source: str = m.SOURCE_MAIL,
    mail_message_id: int | None = None,
    override: dict | None = None,
    dry_run: bool = False,
    use_ai: bool = False,
) -> dict:
    """识别单个附件并落库。返回 {ok, invoice_id, recognized, note, duplicate}。

    dry_run=True 时只识别不写库，供前端「先看看能识别成什么样」。
    use_ai=True 时，本地规则没覆盖到的关键字段会交给大模型再读一遍
    （只补空缺、不覆盖已有值——本地规则命中的字段比模型更可信）。
    """
    result = rec.recognize(filename, data, subject=subject, body=body, include_text=True)

    ai_info: dict | None = None
    if use_ai:
        enriched = ai_service.enrich_invoice(
            db, user, filename=filename, data=data, local=result,
            text=result.get("text") or "",
        )
        ai_info = enriched.get("info")
        applied = enriched.get("applied") or []
        for key in applied:
            result[key] = enriched["fields"][key]
            result.setdefault("field_sources", {})[key] = "ai"
        if applied:
            # 有 AI 参与时把置信度抬到「可入账」档，并在来源上标注，便于事后追查
            result["confidence"] = max(float(result.get("confidence") or 0), 0.9)
            result["source"] = _tag_source(result.get("source"), "ai")

    override = override or {}
    fields = {
        "invoice_no": _pick(override, "invoice_no", result.get("invoice_no")) or None,
        "invoice_code": _pick(override, "invoice_code", result.get("invoice_code")) or None,
        "invoice_type": _pick(override, "invoice_type", result.get("invoice_type")) or rec.DEFAULT_TYPE,
        "amount": _pick(override, "amount", result.get("amount")),
        "tax_amount": _pick(override, "tax_amount", result.get("tax_amount")),
        "tax_rate": _pick(override, "tax_rate", result.get("tax_rate")),
        "invoice_date": _pick(override, "invoice_date", result.get("invoice_date")) or None,
        "seller_name": _pick(override, "seller_name", result.get("seller_name")) or None,
        # 税号早先在识别结果里有、入账时却没落库，导致台账里购销双方税号永远是空——
        # 这是「识别信息不全」最隐蔽的一处，务必与上面的名称成对处理
        "seller_tax_no": _pick(override, "seller_tax_no", result.get("seller_tax_no")) or None,
        "buyer_name": _pick(override, "buyer_name", result.get("buyer_name")) or None,
        "buyer_tax_no": _pick(override, "buyer_tax_no", result.get("buyer_tax_no")) or None,
    }

    notes: list[str] = []
    recognized = bool(result.get("invoice_no"))
    if not recognized:
        notes.append("未识别出发票号码，已以占位号入账，请人工补录")
    if result.get("ocr"):
        info = result["ocr"]
        notes.append(
            f"OCR 识别（{info.get('backend')}，{info.get('lines', '?')} 行"
            + (f"，平均置信度 {info['avg_score']}" if info.get("avg_score") else "")
            + "）"
        )
    if ai_info and ai_info.get("used"):
        applied = ai_info.get("applied") or []
        notes.append(
            (f"AI（{ai_info.get('model')}）补全了 {len(applied)} 项：" + "、".join(applied)
             + ("（按图片识别）" if ai_info.get("by_vision") else ""))
            if applied else f"AI（{ai_info.get('model')}）复核未发现可补字段"
        )
    elif ai_info and ai_info.get("reason"):
        notes.append(f"未启用 AI 兜底：{ai_info['reason']}")
    if result.get("confidence") and result["confidence"] < LOW_CONFIDENCE:
        notes.append(f"识别置信度偏低（{result['confidence']}），请人工复核")
    if result.get("source") not in (None, "none"):
        notes.append(f"识别来源：{result['source']}")
    still = [k for k in ("invoice_no", "amount", "invoice_date", "seller_name") if not result.get(k)]
    if still:
        notes.append("仍未识别：" + "、".join(_FIELD_LABELS.get(k, k) for k in still))

    duplicate = False
    if fields["invoice_no"]:
        exists = (
            db.query(m.Invoice)
            .filter(m.Invoice.invoice_no == fields["invoice_no"])
            .first()
        )
        if exists:
            duplicate = True
            notes.append(f"发票号码已存在（{exists.invoice_no}，单号 {exists.reimbursement.code if exists.reimbursement else '未关联'}），疑似重复报销")

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "filename": filename,
            "recognized": recognized,
            "duplicate": duplicate,
            "confidence": result.get("confidence", 0.0),
            "source": result.get("source"),
            "fields": fields,
            "field_sources": result.get("field_sources") or {},
            "ocr": result.get("ocr"),
            "ai": ai_info,
            "note": "；".join(notes),
        }

    # 先落盘再建记录：文件写失败就不该产生一条指向空文件的发票
    try:
        saved = fs.save_bytes(filename, data)
    except fs.FileRejected as exc:
        return {"ok": False, "filename": filename, "recognized": recognized, "error": str(exc), "note": str(exc)}

    invoice = m.Invoice(
        invoice_no=fields["invoice_no"] or _placeholder_no(),
        invoice_code=fields["invoice_code"],
        invoice_type=fields["invoice_type"],
        amount=fields["amount"] or 0,
        tax_amount=fields["tax_amount"] or 0,
        tax_rate=fields["tax_rate"] or 0,
        invoice_date=_as_date(fields["invoice_date"]),
        seller_name=fields["seller_name"],
        seller_tax_no=fields["seller_tax_no"],
        buyer_name=fields["buyer_name"],
        buyer_tax_no=fields["buyer_tax_no"],
        category_id=override.get("category_id"),
        file_name=saved["filename"],
        created_by=user.username if user else None,
        source=source,
        recognize_score=result.get("confidence") or None,
        recognize_from=result.get("source"),
        mail_message_id=mail_message_id,
        remark="；".join(notes) or None,
    )
    db.add(invoice)
    db.flush()

    db.add(
        m.InvoiceAttachment(
            invoice_id=invoice.id,
            filename=saved["filename"],
            stored_name=saved["stored_name"],
            size=saved["size"],
            mime=saved["mime"],
            uploaded_by=(user.name if user else "邮箱收票"),
        )
    )
    return {
        "ok": True,
        "filename": filename,
        "invoice_id": invoice.id,
        "invoice_no": invoice.invoice_no,
        "amount": float(invoice.amount or 0),
        "recognized": recognized,
        "duplicate": duplicate,
        "confidence": result.get("confidence", 0.0),
        "source": result.get("source"),
        "field_sources": result.get("field_sources") or {},
        "ocr": result.get("ocr"),
        "ai": ai_info,
        "note": "；".join(notes),
    }


def _as_date(value):
    from datetime import date as _date

    if not value:
        return None
    if isinstance(value, _date):
        return value
    try:
        return _date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _import_messages(
    db: Session,
    user: m.AppUser | None,
    account: m.MailAccount,
    mails: list[dict],
    use_ai: bool | None = None,
) -> dict:
    """把一批邮件写进库，返回汇总。同一封邮件只处理一次（按 Message-ID 去重）。

    use_ai 为 None 时读系统参数 `ai_recognize_on_ingest`（默认关）。
    自动收票可能一次几十封，逐封调大模型既慢又费，所以默认不开，
    由管理员在「系统参数」里按需打开。
    """
    if use_ai is None:
        use_ai = ap.get_int(db, "ai_recognize_on_ingest", 0) == 1
    created = 0
    skipped = 0
    failed = 0
    details: list[str] = []

    for mail in mails:
        key = mail.get("message_id") or f"account:{account.id}:uid:{mail.get('uid')}"
        key = key[:255]
        if db.query(m.MailMessage).filter(m.MailMessage.message_id == key).first():
            skipped += 1
            details.append(f"已处理过，跳过：{mail.get('subject') or key}")
            continue

        row = m.MailMessage(
            account_id=account.id,
            uid=(mail.get("uid") or "")[:32] or None,
            message_id=key,
            subject=(mail.get("subject") or "")[:255] or None,
            sender=(mail.get("sender") or "")[:255] or None,
            sent_at=_parse_dt(mail.get("sent_at")),
            attachment_count=len(mail.get("attachments") or []),
            created_by=user.username if user else None,
        )
        db.add(row)
        db.flush()

        imported, skip_one, fail_one = 0, 0, 0
        notes: list[str] = []
        for att in mail.get("attachments") or []:
            name = att["filename"]
            # 明显是签名图/说明文档的跳过，别把收件箱变成垃圾场
            if not rec.looks_like_invoice_name(name) and not att["data"][:4] in (b"%PDF", b"PK\x03\x04"):
                skip_one += 1
                notes.append(f"{name}：文件名不像发票，已跳过")
                continue
            try:
                got = store_one(
                    db, user=user, filename=name, data=att["data"],
                    subject=mail.get("subject") or "", body=mail.get("text") or "",
                    source=m.SOURCE_MAIL, mail_message_id=row.id, use_ai=use_ai,
                )
            except Exception as exc:  # noqa: BLE001
                fail_one += 1
                notes.append(f"{name}：入库失败 {exc}")
                continue
            if got.get("ok"):
                imported += 1
                notes.append(f"{name}：{'已识别' if got['recognized'] else '占位入账'}"
                             f"{'（疑似重复）' if got['duplicate'] else ''}")
            else:
                fail_one += 1
                notes.append(f"{name}：{got.get('error')}")

        if not mail.get("attachments"):
            row.status = m.IMPORT_SKIPPED
            notes.append("无可用附件")
        elif fail_one and not imported:
            row.status = m.IMPORT_FAILED
        elif fail_one or skip_one:
            row.status = m.IMPORT_PARTIAL
        else:
            row.status = m.IMPORT_OK

        row.imported_count = imported
        row.skipped_count = skip_one
        row.detail = "；".join(notes)[:2000] or None
        created += imported
        skipped += skip_one
        failed += fail_one
        details.append(f"{mail.get('subject') or key}：入库 {imported}，跳过 {skip_one}，失败 {fail_one}")

    return {"imported": created, "skipped": skipped, "failed": failed, "details": details[:50]}


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@router.post("/mail-accounts/{oid}/sync", response_model=dict, dependencies=[Depends(_staff)])
def sync_mail_account(
    oid: int,
    request: Request,
    limit: int | None = Query(None, ge=1, le=200),
    user: m.AppUser = Depends(_staff),
    db: Session = Depends(get_db),
):
    """立即收票：拉取 → 识别 → 入账。返回这次收到的明细，前端直接展示。"""
    obj = db.get(m.MailAccount, oid)
    if not obj:
        raise HTTPException(404, "邮箱账号不存在")
    if not obj.active:
        raise HTTPException(400, "该邮箱账号已停用")

    cfg = _account_cfg(obj)
    if not cfg["password"]:
        obj.last_sync_status = m.IMPORT_FAILED
        obj.last_sync_detail = "邮箱密码为空或 MAIL_SECRET 变更后无法解密，请重新填写密码"
        obj.last_sync_at = datetime.now()
        db.commit()
        raise HTTPException(400, obj.last_sync_detail)

    try:
        fetched = mailbox.fetch(cfg, limit=limit or obj.max_per_sync)
    except mailbox.MailboxError as exc:
        obj.last_sync_status = m.IMPORT_FAILED
        obj.last_sync_detail = str(exc)[:1000]
        obj.last_sync_at = datetime.now()
        db.commit()
        raise HTTPException(400, str(exc))

    summary = _import_messages(db, user, obj, fetched["messages"])
    obj.last_sync_at = datetime.now()
    obj.imported_total = (obj.imported_total or 0) + summary["imported"]
    obj.last_sync_status = m.IMPORT_OK if not summary["failed"] else m.IMPORT_PARTIAL
    obj.last_sync_detail = (
        f"扫描 {fetched['scanned']} 封，处理 {len(fetched['messages'])} 封，"
        f"入库 {summary['imported']} 张，跳过 {summary['skipped']}，失败 {summary['failed']}"
    )
    db.add(
        m.AuditLog(
            user_id=user.id, username=user.username, role=user.role,
            action="邮箱收票", entity="mail_account", entity_id=str(oid),
            method=request.method, path=request.url.path, status_code=200,
            ip=sec._client_ip(request), detail=obj.last_sync_detail,
        )
    )
    db.commit()
    return {
        "ok": True,
        "account": obj.name,
        "scanned": fetched["scanned"],
        "processed": len(fetched["messages"]),
        "imported": summary["imported"],
        "skipped": summary["skipped"],
        "failed": summary["failed"],
        "details": summary["details"],
    }


@router.post("/inbox/sync-all", response_model=dict, dependencies=[Depends(_staff)])
def sync_all(
    request: Request,
    user: m.AppUser = Depends(_staff),
    db: Session = Depends(get_db),
):
    """一键收取所有启用的邮箱。单个账号失败不影响其它账号。"""
    accounts = db.query(m.MailAccount).filter(m.MailAccount.active.is_(True)).all()
    if not accounts:
        raise HTTPException(400, "还没有启用中的邮箱账号")
    results = []
    for acc in accounts:
        cfg = _account_cfg(acc)
        if not cfg["password"]:
            results.append({"account": acc.name, "ok": False, "message": "密码缺失，请重新填写"})
            continue
        try:
            fetched = mailbox.fetch(cfg, limit=acc.max_per_sync)
        except mailbox.MailboxError as exc:
            acc.last_sync_at = datetime.now()
            acc.last_sync_status = m.IMPORT_FAILED
            acc.last_sync_detail = str(exc)[:1000]
            results.append({"account": acc.name, "ok": False, "message": str(exc)})
            continue
        summary = _import_messages(db, user, acc, fetched["messages"])
        acc.last_sync_at = datetime.now()
        acc.imported_total = (acc.imported_total or 0) + summary["imported"]
        acc.last_sync_status = m.IMPORT_OK if not summary["failed"] else m.IMPORT_PARTIAL
        acc.last_sync_detail = f"入库 {summary['imported']} 张，跳过 {summary['skipped']}，失败 {summary['failed']}"
        results.append({"account": acc.name, "ok": True, "imported": summary["imported"], "message": acc.last_sync_detail})
    db.commit()
    total = sum(r.get("imported", 0) for r in results)
    return {"ok": True, "accounts": len(accounts), "imported": total, "results": results}


# ------------------------------------------------------------------ 收件记录


@router.get("/inbox/messages", response_model=dict, dependencies=[Depends(_staff)])
def list_messages(
    q: str | None = None,
    status: str | None = None,
    account_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(m.MailMessage)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(m.MailMessage.subject.like(like), m.MailMessage.sender.like(like)))
    if status:
        query = query.filter(m.MailMessage.status == status)
    if account_id:
        query = query.filter(m.MailMessage.account_id == account_id)
    if date_from:
        query = query.filter(func.date(m.MailMessage.created_at) >= date_from)
    if date_to:
        query = query.filter(func.date(m.MailMessage.created_at) <= date_to)

    total = query.count()
    rows = (
        query.order_by(m.MailMessage.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    summary = {
        "total": total,
        "imported": db.query(func.coalesce(func.sum(m.MailMessage.imported_count), 0)).scalar() or 0,
        "failed": db.query(m.MailMessage).filter(m.MailMessage.status == m.IMPORT_FAILED).count(),
    }
    return {"total": total, "page": page, "page_size": page_size, "summary": summary,
            "items": [message_out(x) for x in rows]}


@router.post("/inbox/messages/batch-delete", response_model=dict, dependencies=[Depends(_staff)])
def delete_messages(payload: dict, db: Session = Depends(get_db)):
    """删除收件记录。只删记录，不动已入账的发票——发票要单独走发票的删除。"""
    ids = [int(i) for i in (payload.get("ids") or []) if str(i).isdigit()]
    if not ids:
        raise HTTPException(400, "请先选择要删除的记录")
    n = db.query(m.MailMessage).filter(m.MailMessage.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "deleted": int(n)}


@router.post("/inbox/messages/purge", response_model=dict, dependencies=[Depends(_admin)])
def purge_messages(payload: dict | None = None, db: Session = Depends(get_db)):
    """清空收件记录（管理员）。已入账的发票不受影响，但同一个邮箱会重新收取历史邮件。"""
    _require_confirm(payload, "清空收件记录")
    n = db.query(m.MailMessage).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "deleted": int(n)}


# ------------------------------------------------------------------ 识别与导入


def _recognize_payload(
    db: Session, user: m.AppUser, name: str, data: bytes, subject: str, body: str, use_ai: bool
) -> dict:
    """本地识别 +（可选）大模型兜底。纯同步函数，由调用方丢进线程池执行。

    为什么不直接在 async 端点里调：OCR 首次要加载模型（数秒），大模型可能要几十秒，
    在事件循环里跑会把整个服务的请求都卡住。
    """
    result = rec.recognize(name, data, subject=subject, body=body, include_text=True)
    ai_info = None
    if use_ai:
        enriched = ai_service.enrich_invoice(
            db, user, filename=name, data=data, local=result, text=result.get("text") or ""
        )
        ai_info = enriched.get("info")
        applied = enriched.get("applied") or []
        for key in applied:
            result[key] = enriched["fields"][key]
            # 逐字段来源也要跟着改：界面靠它标出「这一项是模型补的」
            result.setdefault("field_sources", {})[key] = "ai"
        if applied:
            result["confidence"] = max(float(result.get("confidence") or 0), 0.9)
            result["source"] = _tag_source(result.get("source"), "ai")
    fields = {k: result.get(k) for k in _FIELD_LABELS}
    missing = [k for k in ("invoice_no", "amount", "invoice_date", "seller_name") if not result.get(k)]
    return {
        "filename": name,
        "size": len(data),
        "recognized": bool(result.get("invoice_no")),
        "confidence": result.get("confidence", 0.0),
        "source": result.get("source"),
        "ocr": result.get("ocr"),
        "ai": ai_info,
        "layers": result.get("layers") or [],
        "field_sources": result.get("field_sources") or {},
        "missing": [_FIELD_LABELS[k] for k in missing],
        "text_preview": (result.get("text") or "")[:4000],
        **fields,
    }


@router.post("/inbox/recognize", response_model=dict, dependencies=[Depends(_staff)])
async def recognize_only(
    file: UploadFile = File(...),
    subject: str = Form(""),
    body: str = Form(""),
    use_ai: bool = Form(True),
    user: m.AppUser = Depends(_staff),
    db: Session = Depends(get_db),
):
    """只识别不落库：给人先看一眼识别成什么样，再决定要不要入账。

    识别分三层：结构化解析（OFD/XML）→ OCR（图片 / 扫描件）→ 大模型兜底。
    前两层本地完成；第三层只在配了模型且 `use_ai=true` 时跑。
    """
    data = await file.read()
    name = (file.filename or "").strip()
    return await asyncio.to_thread(_recognize_payload, db, user, name, data, subject, body, use_ai)


def _opt_str(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _opt_float(value: str | None) -> float | None:
    text = (value or "").strip().replace(",", "").replace("¥", "").replace("￥", "")
    if not text:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _opt_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


@router.post("/inbox/import", response_model=dict, dependencies=[Depends(_staff)])
async def import_file(
    request: Request,
    file: UploadFile = File(...),
    subject: str = Form(""),
    body: str = Form(""),
    dry_run: bool = Form(False),
    invoice_no: str | None = Form(None),
    invoice_code: str | None = Form(None),
    invoice_type: str | None = Form(None),
    amount: str | None = Form(None),
    tax_amount: str | None = Form(None),
    tax_rate: str | None = Form(None),
    invoice_date: str | None = Form(None),
    seller_name: str | None = Form(None),
    seller_tax_no: str | None = Form(None),
    buyer_name: str | None = Form(None),
    buyer_tax_no: str | None = Form(None),
    category_id: str | None = Form(None),
    use_ai: bool = Form(True),
    user: m.AppUser = Depends(_staff),
    db: Session = Depends(get_db),
):
    """手工上传一张电子发票：识别 + 入账。

    表单里传来的字段一律**覆盖**识别结果——识别只是替人打草稿，
    人工在预览页改过的值必须以人工为准（原先只允许改号码，识别错的
    金额/日期/购销方在入账前来不及纠正，等于白识别）。

    参数默认 None 而不是 ""，是为了区分「没传」与「传了空值」：
    没传 = 沿用识别结果，传空 = 人工确认这里就该是空的。
    """
    data = await file.read()
    name = (file.filename or "").strip()

    override: dict = {}
    for key, raw in (
        ("invoice_no", invoice_no),
        ("invoice_code", invoice_code),
        ("invoice_type", invoice_type),
        ("invoice_date", invoice_date),
        ("seller_name", seller_name),
        ("seller_tax_no", seller_tax_no),
        ("buyer_name", buyer_name),
        ("buyer_tax_no", buyer_tax_no),
    ):
        if raw is not None:
            override[key] = _opt_str(raw)
    for key, raw in (("amount", amount), ("tax_amount", tax_amount), ("tax_rate", tax_rate)):
        if raw is not None:
            override[key] = _opt_float(raw)
    if category_id is not None:
        override["category_id"] = _opt_int(category_id)
    # OCR 与大模型都可能耗时，整段落库流程丢线程池，别把事件循环堵住
    got = await asyncio.to_thread(
        store_one, db, user=user, filename=name, data=data, subject=subject, body=body,
        source=m.SOURCE_IMPORT, override=override, dry_run=dry_run, use_ai=use_ai,
    )
    if not got.get("ok") and got.get("error"):
        db.rollback()
        raise HTTPException(400, got["error"])
    if not dry_run:
        db.add(
            m.AuditLog(
                user_id=user.id, username=user.username, role=user.role,
                action="导入电子发票", entity="invoice", entity_id=str(got.get("invoice_id")),
                method=request.method, path=request.url.path, status_code=200,
                ip=sec._client_ip(request), detail=f"{name} -> {got.get('invoice_no')} {got.get('note') or ''}"[:1000],
            )
        )
        db.commit()
    return got


@router.post("/inbox/ingest", response_model=dict)
async def ingest(
    request: Request,
    files: list[UploadFile] = File(...),
    subject: str = Form(""),
    sender: str = Form(""),
    date: str = Form(""),
    db: Session = Depends(get_db),
):
    """外部通道投递口（Agent Mail / 自建脚本 / 邮件网关都用它）。

    鉴权二选一：
      - `X-Ingest-Token: <INBOX_INGEST_TOKEN>`（适合无人值守的脚本）
      - `Authorization: Bearer <登录 token>`（适合人工触发的自动化）
    """
    user = _ingest_user(request, db)

    # Message-ID 去重：外部脚本重试很常见，重复投递必须幂等，
    # 否则唯一约束会炸成 500，对方还会一直重试。
    incoming_id = (request.headers.get("X-Message-Id") or "").strip()
    if incoming_id:
        key = ("ingest:" + incoming_id)[:255]
        existed = db.query(m.MailMessage).filter(m.MailMessage.message_id == key).first()
        if existed:
            return {
                "ok": True,
                "duplicate": True,
                "message_id": existed.message_id,
                "imported": 0,
                "skipped": len(files),
                "failed": 0,
                "results": [],
                "message": "该邮件已投递过，未重复入库",
            }

    row = m.MailMessage(
        account_id=None,
        uid=None,
        message_id=("ingest:" + (incoming_id or secrets.token_hex(12)))[:255],
        subject=(subject or "")[:255] or None,
        sender=(sender or "")[:255] or None,
        sent_at=_parse_dt(date),
        attachment_count=len(files),
        created_by=user.username if user else "外部投递",
    )
    db.add(row)
    db.flush()

    imported, skipped, failed = 0, 0, 0
    results: list[dict] = []
    for f in files:
        data = await f.read()
        name = (f.filename or "").strip()
        if not name:
            failed += 1
            results.append({"filename": "", "ok": False, "error": "缺少文件名"})
            continue
        got = await asyncio.to_thread(
            store_one, db, user=user, filename=name, data=data, subject=subject, body="",
            source=m.SOURCE_MAIL, mail_message_id=row.id,
            use_ai=ap.get_int(db, "ai_recognize_on_ingest", 0) == 1,
        )
        if got.get("ok"):
            imported += 1
        else:
            failed += 1
        results.append(got)

    row.imported_count = imported
    row.skipped_count = skipped
    row.status = m.IMPORT_OK if not failed else (m.IMPORT_PARTIAL if imported else m.IMPORT_FAILED)
    row.detail = f"外部投递：入库 {imported}，失败 {failed}"
    db.add(
        m.AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else "外部投递",
            role=user.role if user else None,
            action="外部投递发票", entity="inbox", entity_id=str(row.id),
            method=request.method, path=request.url.path, status_code=200,
            ip=sec._client_ip(request), detail=f"{subject or '-'}：入库 {imported}，失败 {failed}",
        )
    )
    db.commit()
    return {"ok": True, "message_id": row.message_id, "imported": imported,
            "skipped": skipped, "failed": failed, "results": results}


def _ingest_user(request: Request, db: Session) -> m.AppUser | None:
    """外部投递鉴权。配了 INBOX_INGEST_TOKEN 才允许无登录态调用。"""
    token = request.headers.get("X-Ingest-Token") or ""
    if INGEST_TOKEN and token and secrets.compare_digest(token, INGEST_TOKEN):
        return None
    try:
        return sec.current_user(request, db)
    except HTTPException:
        if not INGEST_TOKEN:
            raise HTTPException(401, "外部投递未启用：请配置 INBOX_INGEST_TOKEN 或使用登录态调用")
        raise HTTPException(401, "投递令牌无效")


def _require_confirm(payload: dict | None, keyword: str) -> None:
    """危险操作要求把确认词原样打一遍，防止误点。"""
    got = ((payload or {}).get("confirm") or "").strip()
    if got != keyword:
        raise HTTPException(400, f"危险操作：请在确认框中原样输入「{keyword}」")
