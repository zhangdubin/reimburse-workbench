"""发票台账：录入 / 查验 / 关联报销单 / 重复报销检测。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from .. import filestore as fs
from .. import models as m
from .. import schemas as s
from .. import security as sec
from .. import serializers as ser
from ..database import get_db

# 发票的登记、修改、查验、关联都属财务动作
_can_write = sec.require_roles(m.ROLE_FINANCE, m.ROLE_ADMIN)
_any_user = sec.current_user


def _ensure_invoice_visible(db: Session, user: m.AppUser, inv: m.Invoice) -> None:
    """单张访问时的越权校验。范围为空表示全局可见。"""
    conds = sec.invoice_scope_conds(user)
    if not conds:
        return
    found = db.query(m.Invoice.id).filter(m.Invoice.id == inv.id, *conds).first()
    if not found:
        raise HTTPException(403, "无权访问该发票")

router = APIRouter(prefix="/api/invoices", tags=["发票"])


def _load(db: Session, oid: int) -> m.Invoice:
    obj = (
        db.query(m.Invoice)
        # populate_existing：会话是 expire_on_commit=False，同一请求内改过
        # reimbursement_id 后，已加载的 relationship 会保留旧值（脏读），
        # 必须强制用库里的最新数据覆盖，响应里才能拿到正确的关联单号。
        .populate_existing()
        .options(
            selectinload(m.Invoice.category),
            selectinload(m.Invoice.reimbursement),
        )
        .filter(m.Invoice.id == oid)
        .first()
    )
    if not obj:
        raise HTTPException(404, "发票不存在")
    return obj


def _sync_reimbursement(db: Session, rid: int | None) -> None:
    if not rid:
        return
    r = db.get(m.Reimbursement, rid)
    if not r:
        return
    r.invoice_count = (
        db.query(func.count(m.Invoice.id)).filter(m.Invoice.reimbursement_id == rid).scalar() or 0
    )


def _find_duplicates(db: Session, invoice_no: str, code: str | None, exclude_id: int | None = None):
    q = db.query(m.Invoice).filter(m.Invoice.invoice_no == invoice_no)
    if code:
        q = q.filter(m.Invoice.invoice_code == code)
    if exclude_id:
        q = q.filter(m.Invoice.id != exclude_id)
    return q.all()


def _remove_files(db: Session, inv: m.Invoice) -> int:
    """删除发票时一并清掉磁盘上的影像文件。

    只删数据库行会把文件永远留在 UPLOAD_DIR 里，时间一长就是一堆孤儿文件。
    """
    rows = db.query(m.InvoiceAttachment).filter(m.InvoiceAttachment.invoice_id == inv.id).all()
    n = 0
    for a in rows:
        if fs.delete_stored(a.stored_name):
            n += 1
    return n


def _purge_invoices(db: Session, user: m.AppUser, ids: list[int]) -> dict:
    """批量删除发票：受可见范围约束，非财务/管理员看不到的票直接拒绝。"""
    conds = sec.invoice_scope_conds(user)
    q = db.query(m.Invoice).filter(m.Invoice.id.in_(ids))
    if conds:
        q = q.filter(*conds)
    rows = q.all()

    found = {r.id for r in rows}
    denied = sorted(set(ids) - found)
    touched = {r.reimbursement_id for r in rows if r.reimbursement_id}
    files = 0
    for r in rows:
        files += _remove_files(db, r)
        db.delete(r)
    db.flush()
    for rid in touched:
        _sync_reimbursement(db, rid)
    db.commit()
    return {
        "ok": True,
        "deleted": len(rows),
        "files_removed": files,
        "denied": denied,
        "denied_count": len(denied),
    }


@router.post("/batch-delete", response_model=dict, dependencies=[Depends(_can_write)])
def batch_delete(payload: dict, user: m.AppUser = Depends(_can_write), db: Session = Depends(get_db)):
    """批量删除发票（含影像文件）。前端多选后调用。"""
    ids = [int(i) for i in (payload.get("ids") or []) if str(i).isdigit()]
    if not ids:
        raise HTTPException(400, "请先勾选要删除的发票")
    if len(ids) > 500:
        raise HTTPException(400, "单次最多删除 500 条，请分批操作")
    return _purge_invoices(db, user, ids)


@router.post("/purge-all", response_model=dict, dependencies=[Depends(_can_write)])
def purge_all(
    payload: dict | None = None,
    only_unlinked: bool = Query(False, description="只清空未关联报销单的散票"),
    user: m.AppUser = Depends(_can_write),
    db: Session = Depends(get_db),
):
    """清空发票台账。默认清全部，可只清散票。

    这样清完还能留个记录可查——删库不留痕的接口不该存在。
    """
    keyword = "清空散票" if only_unlinked else "清空全部发票"
    got = ((payload or {}).get("confirm") or "").strip()
    if got != keyword:
        raise HTTPException(400, f"危险操作：请在确认框中原样输入「{keyword}」")

    q = db.query(m.Invoice)
    if only_unlinked:
        q = q.filter(m.Invoice.reimbursement_id.is_(None))
    rows = q.all()
    files = 0
    touched = {r.reimbursement_id for r in rows if r.reimbursement_id}
    for r in rows:
        files += _remove_files(db, r)
        db.delete(r)
    db.flush()
    for rid in touched:
        _sync_reimbursement(db, rid)
    db.commit()
    return {"ok": True, "deleted": len(rows), "files_removed": files}


@router.get("", response_model=s.Page)
def list_invoices(
    q: str | None = None,
    check_status: str | None = None,
    invoice_type: str | None = None,
    category_id: int | None = None,
    reimbursement_id: int | None = None,
    unlinked: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    sort: str = "id_desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: m.AppUser = Depends(_any_user),
    db: Session = Depends(get_db),
):
    query = (
        db.query(m.Invoice)
        .options(selectinload(m.Invoice.category), selectinload(m.Invoice.reimbursement))
        .filter(*sec.invoice_scope_conds(user))
    )
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                m.Invoice.invoice_no.like(like),
                m.Invoice.invoice_code.like(like),
                m.Invoice.seller_name.like(like),
                m.Invoice.buyer_name.like(like),
                m.Invoice.remark.like(like),
            )
        )
    if check_status:
        query = query.filter(m.Invoice.check_status.in_(check_status.split(",")))
    if invoice_type:
        query = query.filter(m.Invoice.invoice_type == invoice_type)
    if category_id:
        query = query.filter(m.Invoice.category_id == category_id)
    if reimbursement_id:
        query = query.filter(m.Invoice.reimbursement_id == reimbursement_id)
    if unlinked:
        query = query.filter(m.Invoice.reimbursement_id.is_(None))
    if date_from:
        query = query.filter(m.Invoice.invoice_date >= date_from)
    if date_to:
        query = query.filter(m.Invoice.invoice_date <= date_to)
    if amount_min is not None:
        query = query.filter(m.Invoice.amount >= amount_min)
    if amount_max is not None:
        query = query.filter(m.Invoice.amount <= amount_max)

    total = query.count()
    order = {
        "id_desc": m.Invoice.id.desc(),
        "id_asc": m.Invoice.id.asc(),
        "amount_desc": m.Invoice.amount.desc(),
        "amount_asc": m.Invoice.amount.asc(),
        "date_desc": m.Invoice.invoice_date.desc().nullslast(),
        "date_asc": m.Invoice.invoice_date.asc().nullslast(),
    }.get(sort, m.Invoice.id.desc())
    rows = query.order_by(order).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [ser.invoice_out(v) for v in rows],
    }


@router.get("/summary", response_model=dict)
def invoice_summary(
    user: m.AppUser = Depends(_any_user),
    db: Session = Depends(get_db),
):
    """发票台账概览：按查验状态与类型汇总，并统计待关联数量。"""
    scope = sec.invoice_scope_conds(user)

    def scoped(q):
        return q.filter(*scope) if scope else q

    by_status = scoped(
        db.query(m.Invoice.check_status, func.count(m.Invoice.id), func.coalesce(func.sum(m.Invoice.amount), 0))
    ).group_by(m.Invoice.check_status).all()
    by_type = scoped(
        db.query(m.Invoice.invoice_type, func.count(m.Invoice.id), func.coalesce(func.sum(m.Invoice.amount), 0))
    ).group_by(m.Invoice.invoice_type).order_by(func.sum(m.Invoice.amount).desc()).all()
    total_amount = scoped(db.query(func.coalesce(func.sum(m.Invoice.amount), 0))).scalar()
    total_count = scoped(db.query(func.count(m.Invoice.id))).scalar()
    tax_amount = scoped(db.query(func.coalesce(func.sum(m.Invoice.tax_amount), 0))).scalar()
    unlinked = scoped(
        db.query(func.count(m.Invoice.id)).filter(m.Invoice.reimbursement_id.is_(None))
    ).scalar()
    dup = _duplicate_list(db, scope)
    return {
        "total_count": total_count,
        "total_amount": ser.money(total_amount),
        "total_tax": ser.money(tax_amount),
        "unlinked_count": unlinked,
        "duplicate_count": len(dup),
        "duplicates": dup,
        "by_status": [
            {"name": r[0], "count": r[1], "amount": ser.money(r[2])} for r in by_status
        ],
        "by_type": [{"name": r[0], "count": r[1], "amount": ser.money(r[2])} for r in by_type],
    }


def _shift_month(key: str, delta: int) -> str:
    total = int(key[:4]) * 12 + (int(key[5:7]) - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


@router.get("/monthly", response_model=list[dict])
def invoice_monthly(
    months: int = Query(6, ge=1, le=36),
    user: m.AppUser = Depends(_any_user),
    db: Session = Depends(get_db),
):
    """按开票月份归集的发票金额与张数（仅统计已填报开票日期的发票）。"""
    today = date.today()
    start_key = _shift_month(today.strftime("%Y-%m"), -(months - 1))
    start_date = date(int(start_key[:4]), int(start_key[5:7]), 1)

    q = (
        db.query(m.Invoice.invoice_date, m.Invoice.amount, m.Invoice.tax_amount)
        .filter(m.Invoice.invoice_date.isnot(None))
        .filter(m.Invoice.invoice_date >= start_date)
    )
    scope = sec.invoice_scope_conds(user)
    rows = (q.filter(*scope) if scope else q).all()

    buckets = {_shift_month(start_key, i): {"amount": 0.0, "tax": 0.0, "count": 0} for i in range(months)}
    for inv_date, amount, tax in rows:
        key = f"{inv_date.year:04d}-{inv_date.month:02d}"
        if key not in buckets:
            continue
        buckets[key]["amount"] += ser.money(amount)
        buckets[key]["tax"] += ser.money(tax)
        buckets[key]["count"] += 1

    return [
        {
            "month": k,
            "amount": ser.money(v["amount"]),
            "tax": ser.money(v["tax"]),
            "count": v["count"],
        }
        for k, v in sorted(buckets.items())
    ]


def _duplicate_list(db: Session, scope: list | None = None) -> list[dict]:
    """同一 发票号码+发票代码 出现多次 = 潜在重复报销。"""
    scope = scope or []
    head = db.query(
        m.Invoice.invoice_no,
        m.Invoice.invoice_code,
        func.count(m.Invoice.id),
        func.coalesce(func.sum(m.Invoice.amount), 0),
    )
    if scope:
        head = head.filter(*scope)
    rows = (
        head.group_by(m.Invoice.invoice_no, m.Invoice.invoice_code)
        .having(func.count(m.Invoice.id) > 1)
        .all()
    )
    out = []
    for no, code, cnt, amt in rows:
        detail_q = (
            db.query(m.Invoice)
            .options(selectinload(m.Invoice.reimbursement))
            .filter(m.Invoice.invoice_no == no)
        )
        if scope:
            detail_q = detail_q.filter(*scope)
        detail = detail_q.all()
        out.append(
            {
                "invoice_no": no,
                "invoice_code": code,
                "count": cnt,
                "amount": ser.money(amt),
                "records": [
                    {
                        "id": v.id,
                        "amount": ser.money(v.amount),
                        "invoice_date": v.invoice_date.isoformat() if v.invoice_date else None,
                        "check_status": v.check_status,
                        "reimbursement_code": v.reimbursement.code if v.reimbursement else None,
                    }
                    for v in detail
                ],
            }
        )
    return out


@router.get("/{oid}", response_model=s.InvoiceOut)
def get_invoice(
    oid: int,
    user: m.AppUser = Depends(_any_user),
    db: Session = Depends(get_db),
):
    obj = _load(db, oid)
    _ensure_invoice_visible(db, user, obj)
    return ser.invoice_out(obj)


@router.post("", response_model=s.InvoiceOut, status_code=201)
def create_invoice(
    payload: s.InvoiceIn,
    user: m.AppUser = Depends(_can_write),
    db: Session = Depends(get_db),
):
    obj = m.Invoice(**payload.model_dump(), created_by=user.username)
    dups = _find_duplicates(db, payload.invoice_no, payload.invoice_code)
    if dups:
        obj.check_status = m.CHECK_BAD
        obj.check_result = f"疑似重复报销：已存在 {len(dups)} 条相同号码的发票记录"
    db.add(obj)
    db.flush()
    _sync_reimbursement(db, obj.reimbursement_id)
    db.commit()
    return ser.invoice_out(_load(db, obj.id))


@router.put("/{oid}", response_model=s.InvoiceOut, dependencies=[Depends(_can_write)])
def update_invoice(oid: int, payload: s.InvoiceIn, db: Session = Depends(get_db)):
    obj = _load(db, oid)
    old_rid = obj.reimbursement_id
    for k, v in payload.model_dump().items():
        setattr(obj, k, v)
    dups = _find_duplicates(db, payload.invoice_no, payload.invoice_code, exclude_id=oid)
    if dups and obj.check_status != m.CHECK_BAD:
        obj.check_status = m.CHECK_BAD
        obj.check_result = f"疑似重复报销：已存在 {len(dups)} 条相同号码的发票记录"
    db.flush()
    _sync_reimbursement(db, old_rid)
    _sync_reimbursement(db, obj.reimbursement_id)
    db.commit()
    return ser.invoice_out(_load(db, obj.id))


@router.delete("/{oid}", dependencies=[Depends(_can_write)])
def delete_invoice(oid: int, db: Session = Depends(get_db)):
    obj = _load(db, oid)
    rid = obj.reimbursement_id
    _remove_files(db, obj)
    db.delete(obj)
    db.flush()
    _sync_reimbursement(db, rid)
    db.commit()
    return {"ok": True, "deleted": oid}


@router.post("/{oid}/check", response_model=s.InvoiceOut, dependencies=[Depends(_can_write)])
def check_invoice(oid: int, db: Session = Depends(get_db)):
    """发票查验（本地规则校验版）。

    真实场景这里应对接税务总局全国增值税发票查验平台或第三方查验服务，
    本工作台先内置一套可用的规则校验，接口形状保持一致便于后续替换。
    """
    obj = _load(db, oid)
    problems: list[str] = []
    if not obj.invoice_no or len(obj.invoice_no.strip()) < 8:
        problems.append("发票号码长度异常")
    if not obj.seller_name:
        problems.append("缺少销售方名称")
    if ser.money(obj.amount) <= 0:
        problems.append("金额必须大于 0")
    if obj.invoice_date is None:
        problems.append("缺少开票日期")
    dups = _find_duplicates(db, obj.invoice_no, obj.invoice_code, exclude_id=oid)
    if dups:
        problems.append(f"疑似重复报销（已有 {len(dups)} 条同号发票）")

    if problems:
        obj.check_status = m.CHECK_BAD
        obj.check_result = "；".join(problems)
    else:
        obj.check_status = m.CHECK_OK
        obj.check_result = "基础校验通过（号码、金额、日期、销方信息完整，无重复记录）"
    db.commit()
    return ser.invoice_out(_load(db, obj.id))


@router.post("/batch-check", response_model=dict, dependencies=[Depends(_can_write)])
def batch_check(ids: list[int] | None = None, db: Session = Depends(get_db)):
    query = db.query(m.Invoice)
    if ids:
        query = query.filter(m.Invoice.id.in_(ids))
    else:
        query = query.filter(m.Invoice.check_status == m.CHECK_UNVERIFIED)
    rows = query.all()
    ok = bad = 0
    for obj in rows:
        res = check_invoice(obj.id, db)
        if res["check_status"] == m.CHECK_OK:
            ok += 1
        else:
            bad += 1
    return {"checked": len(rows), "ok": ok, "bad": bad}


@router.post("/{oid}/link", response_model=s.InvoiceOut, dependencies=[Depends(_can_write)])
def link_invoice(
    oid: int,
    reimbursement_id: int | None = None,
    item_id: int | None = None,
    db: Session = Depends(get_db),
):
    obj = _load(db, oid)
    old_rid = obj.reimbursement_id
    if reimbursement_id is not None:
        r = db.get(m.Reimbursement, reimbursement_id)
        if not r:
            raise HTTPException(404, "报销单不存在")
        if item_id:
            item = db.get(m.ReimbursementItem, item_id)
            if not item or item.reimbursement_id != reimbursement_id:
                raise HTTPException(400, "明细行不属于该报销单")
        obj.reimbursement_id = reimbursement_id
        obj.item_id = item_id
        # 报销单金额与发票金额的一致性提示
        if item_id:
            item_obj = db.get(m.ReimbursementItem, item_id)
            if abs(ser.money(item_obj.amount) - ser.money(obj.amount)) > 0.01:
                obj.remark = (obj.remark or "") + f"｜注意：发票金额与明细行金额不一致"
        if r.status != m.ST_DRAFT:
            obj.check_result = "已关联到非草稿报销单，请确认是否需要重新审批"
    else:
        obj.reimbursement_id = None
        obj.item_id = None
    db.flush()
    _sync_reimbursement(db, old_rid)
    _sync_reimbursement(db, obj.reimbursement_id)
    db.commit()
    return ser.invoice_out(_load(db, obj.id))
