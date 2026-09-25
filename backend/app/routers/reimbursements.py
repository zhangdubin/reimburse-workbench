"""报销单：创建 / 编辑 / 查询 / 状态流转（含分级审批与数据权限）。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from .. import approvals as ap
from .. import models as m
from .. import schemas as s
from .. import security as sec
from .. import serializers as ser
from ..database import get_db

router = APIRouter(prefix="/api/reimbursements", tags=["报销单"])

EDITABLE_STATUSES = {m.ST_DRAFT, m.ST_REJECTED}

# 可代他人建单/改单的角色（财务代办、管理员维护）
DELEGATE_ROLES = (m.ROLE_ADMIN, m.ROLE_FINANCE)


def _next_code(db: Session) -> str:
    prefix = "BX" + datetime.now().strftime("%Y%m")
    last = (
        db.query(m.Reimbursement.code)
        .filter(m.Reimbursement.code.like(f"{prefix}%"))
        .order_by(m.Reimbursement.code.desc())
        .first()
    )
    seq = int(last[0][-4:]) + 1 if last else 1
    return f"{prefix}{seq:04d}"


def _load(db: Session, oid: int) -> m.Reimbursement:
    obj = (
        db.query(m.Reimbursement)
        # 同 invoices._load：会话 expire_on_commit=False，改单后必须强制刷新，
        # 否则响应里的关联对象（申请人/部门/明细）可能是改前的旧值。
        .populate_existing()
        .options(
            selectinload(m.Reimbursement.items).selectinload(m.ReimbursementItem.invoices),
            selectinload(m.Reimbursement.items).selectinload(m.ReimbursementItem.category),
            selectinload(m.Reimbursement.invoices),
            selectinload(m.Reimbursement.logs),
            selectinload(m.Reimbursement.applicant),
            selectinload(m.Reimbursement.department),
            selectinload(m.Reimbursement.project),
            selectinload(m.Reimbursement.customer),
        )
        .filter(m.Reimbursement.id == oid)
        .first()
    )
    if not obj:
        raise HTTPException(404, "报销单不存在")
    return obj


def _recalc(db: Session, r: m.Reimbursement) -> None:
    total = sum(ser.money(i.amount) for i in r.items)
    r.total_amount = ser.money(total)
    inv_ids = [v.id for i in r.items for v in i.invoices]
    linked = db.query(func.count(m.Invoice.id)).filter(m.Invoice.reimbursement_id == r.id).scalar() or 0
    r.invoice_count = len(set(inv_ids)) or linked
    dates = [i.occur_date for i in r.items if i.occur_date]
    if dates:
        r.occur_start = min(dates)
        r.occur_end = max(dates)


def _log(db, r, action, operator=None, comment=None, from_status=None, to_status=None, level=None):
    db.add(
        m.ApprovalLog(
            reimbursement_id=r.id,
            action=action,
            operator=operator,
            comment=comment,
            from_status=from_status,
            to_status=to_status,
            level=level,
        )
    )


def _replace_items(db: Session, r: m.Reimbursement, items: list[s.ItemIn]) -> None:
    """草稿可整体重写明细；已被发票关联的明细行保留关联关系。"""
    existing = {i.id: i for i in r.items}
    keep_ids = {it.id for it in items if it.id}
    for oid, obj in existing.items():
        if oid not in keep_ids:
            # 解除发票关联后删除，避免发票变成孤儿
            for v in list(obj.invoices):
                v.item_id = None
            db.delete(obj)
    r.items = [i for i in r.items if i.id in keep_ids]
    for it in items:
        data = it.model_dump(exclude={"id"})
        if it.id and it.id in existing:
            for k, v in data.items():
                setattr(existing[it.id], k, v)
        else:
            r.items.append(m.ReimbursementItem(**data))


def _ensure_owner_or_delegate(user: m.AppUser, r: m.Reimbursement) -> None:
    if user.role in DELEGATE_ROLES:
        return
    if user.employee_id is None or r.applicant_id != user.employee_id:
        raise HTTPException(403, "只能操作自己提交的报销单")


def _next_pending_level(r: m.Reimbursement) -> int:
    """当前等待审批的级别。"""
    return min(r.approved_level + 1, max(r.required_level, 1))


# ------------------------------------------------------------------ 查询
@router.get("", response_model=s.Page)
def list_reimbursements(
    q: str | None = None,
    status: str | None = None,
    department_id: int | None = None,
    applicant_id: int | None = None,
    project_id: int | None = None,
    customer_id: int | None = None,
    category_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    pending_level: int | None = Query(None, description="按当前待审级别筛选 1/2"),
    sort: str = "id_desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    query = (
        db.query(m.Reimbursement)
        .options(
            selectinload(m.Reimbursement.applicant),
            selectinload(m.Reimbursement.department),
            selectinload(m.Reimbursement.project),
            selectinload(m.Reimbursement.customer),
        )
        # 数据可见范围必须先于任何业务筛选生效，避免越权读到别人的单
        .filter(*sec.scope_conds(user))
    )
    if q:
        like = f"%{q}%"
        sub_applicant = db.query(m.Employee.id).filter(m.Employee.name.like(like)).subquery()
        query = query.filter(
            or_(
                m.Reimbursement.code.like(like),
                m.Reimbursement.title.like(like),
                m.Reimbursement.purpose.like(like),
                m.Reimbursement.applicant_id.in_(db.query(sub_applicant.c.id)),
            )
        )
    if status:
        query = query.filter(m.Reimbursement.status.in_(status.split(",")))
    if department_id:
        query = query.filter(m.Reimbursement.department_id == department_id)
    if applicant_id:
        query = query.filter(m.Reimbursement.applicant_id == applicant_id)
    if project_id:
        query = query.filter(m.Reimbursement.project_id == project_id)
    if customer_id:
        query = query.filter(m.Reimbursement.customer_id == customer_id)
    if category_id:
        sub = db.query(m.ReimbursementItem.reimbursement_id).filter(
            m.ReimbursementItem.category_id == category_id
        )
        query = query.filter(m.Reimbursement.id.in_(sub))
    if date_from:
        query = query.filter(
            or_(m.Reimbursement.occur_start >= date_from, m.Reimbursement.occur_start.is_(None))
        )
    if date_to:
        query = query.filter(
            or_(m.Reimbursement.occur_end <= date_to, m.Reimbursement.occur_end.is_(None))
        )
    if amount_min is not None:
        query = query.filter(m.Reimbursement.total_amount >= amount_min)
    if amount_max is not None:
        query = query.filter(m.Reimbursement.total_amount <= amount_max)
    if pending_level:
        # 待审级别是「已通过级数 + 1」，跨库没有统一表达式，用区间近似后在 Python 侧收口
        query = query.filter(m.Reimbursement.status == m.ST_PENDING)

    total = query.count()
    order = {
        "id_desc": m.Reimbursement.id.desc(),
        "id_asc": m.Reimbursement.id.asc(),
        "amount_desc": m.Reimbursement.total_amount.desc(),
        "amount_asc": m.Reimbursement.total_amount.asc(),
        "date_desc": m.Reimbursement.occur_start.desc().nullslast(),
        "date_asc": m.Reimbursement.occur_start.asc().nullslast(),
    }.get(sort, m.Reimbursement.id.desc())

    rows = query.order_by(order).offset((page - 1) * page_size).limit(page_size).all()
    if pending_level:
        rows = [r for r in rows if _next_pending_level(r) == pending_level]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [ser.reimbursement_out(r) for r in rows],
    }


@router.get("/{oid}", response_model=s.ReimbursementDetail)
def get_reimbursement(
    oid: int,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    sec.ensure_can_view(user, r)
    return ser.reimbursement_detail(r)


# ------------------------------------------------------------------ 写操作
@router.post("", response_model=s.ReimbursementDetail, status_code=201)
def create_reimbursement(
    payload: s.ReimbursementIn,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    data = payload.model_dump(exclude={"items"})
    if user.role not in DELEGATE_ROLES:
        # 非代办角色只能给自己建单，忽略前端传来的 applicant_id
        if user.employee_id is None:
            raise HTTPException(400, "当前账号未绑定员工，无法建单，请联系管理员")
        data["applicant_id"] = user.employee_id
    if data.get("applicant_id") and not data.get("department_id"):
        emp = db.get(m.Employee, data["applicant_id"])
        if emp:
            data["department_id"] = emp.department_id
    if not data.get("applicant_id"):
        raise HTTPException(400, "请指定申请人")

    r = m.Reimbursement(code=_next_code(db), status=m.ST_DRAFT, **data)
    db.add(r)
    db.flush()
    for it in payload.items:
        r.items.append(m.ReimbursementItem(**it.model_dump(exclude={"id"})))
    _recalc(db, r)
    _log(db, r, "创建", operator=user.name, to_status=m.ST_DRAFT, comment="创建报销单")
    db.commit()
    return ser.reimbursement_detail(_load(db, r.id))


@router.put("/{oid}", response_model=s.ReimbursementDetail)
def update_reimbursement(
    oid: int,
    payload: s.ReimbursementIn,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    sec.ensure_can_view(user, r)
    _ensure_owner_or_delegate(user, r)
    if r.status not in EDITABLE_STATUSES:
        raise HTTPException(400, f"当前状态「{r.status}」不可编辑，请先撤回或驳回")
    data = payload.model_dump(exclude={"items"})
    if user.role not in DELEGATE_ROLES:
        # 申请人不能通过改单把单据挂到别人名下
        data.pop("applicant_id", None)
    for k, v in data.items():
        setattr(r, k, v)
    _replace_items(db, r, payload.items)
    _recalc(db, r)
    db.commit()
    return ser.reimbursement_detail(_load(db, r.id))


@router.delete("/{oid}")
def delete_reimbursement(
    oid: int,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    sec.ensure_can_view(user, r)
    _ensure_owner_or_delegate(user, r)
    if r.status not in EDITABLE_STATUSES:
        raise HTTPException(400, f"当前状态「{r.status}」不可删除")
    for v in r.invoices:
        v.reimbursement_id = None
        v.item_id = None
    db.delete(r)
    db.commit()
    return {"ok": True, "deleted": oid}


def _detach_invoices(db: Session, r: m.Reimbursement) -> None:
    """删单前先解绑发票：发票是资产凭证，不能跟着报销单一起消失。"""
    for v in db.query(m.Invoice).filter(m.Invoice.reimbursement_id == r.id).all():
        v.reimbursement_id = None
        v.item_id = None


@router.post("/batch-delete", response_model=dict)
def batch_delete_reimbursements(
    payload: dict,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    """批量删除报销单。逐条校验权限与状态，能删的删、不能删的给出原因。"""
    ids = [int(i) for i in (payload.get("ids") or []) if str(i).isdigit()]
    if not ids:
        raise HTTPException(400, "请先勾选要删除的报销单")
    if len(ids) > 500:
        raise HTTPException(400, "单次最多删除 500 条，请分批操作")

    deleted: list[int] = []
    skipped: list[dict] = []
    for oid in ids:
        r = db.get(m.Reimbursement, oid)
        if not r:
            skipped.append({"id": oid, "reason": "单据不存在"})
            continue
        if not sec.can_view(user, r):
            skipped.append({"id": oid, "code": r.code, "reason": "无权删除该单据"})
            continue
        if user.role not in DELEGATE_ROLES and (user.employee_id is None or r.applicant_id != user.employee_id):
            skipped.append({"id": oid, "code": r.code, "reason": "只能删除自己提交的单据"})
            continue
        if r.status not in EDITABLE_STATUSES:
            skipped.append({"id": oid, "code": r.code, "reason": f"状态「{r.status}」不可删除"})
            continue
        _detach_invoices(db, r)
        db.delete(r)
        db.flush()
        deleted.append(oid)

    db.commit()
    return {"ok": True, "deleted": len(deleted), "deleted_ids": deleted,
            "skipped": skipped, "skipped_count": len(skipped)}


@router.post("/purge-all", response_model=dict)
def purge_all_reimbursements(
    payload: dict | None = None,
    user: m.AppUser = Depends(sec.require_roles(m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    """清空报销单（管理员）。发票不会被删，只会解除关联，避免误删凭证。"""
    got = ((payload or {}).get("confirm") or "").strip()
    if got != "清空全部报销单":
        raise HTTPException(400, "危险操作：请在确认框中原样输入「清空全部报销单」")

    rows = db.query(m.Reimbursement).all()
    for r in rows:
        _detach_invoices(db, r)
        db.delete(r)
    db.commit()
    return {"ok": True, "deleted": len(rows)}


def _transition(r: m.Reimbursement, action: str) -> tuple[str, str]:
    """返回 (from_status, to_status)，不合法则抛错。"""
    cur = r.status
    table = {
        "submit": {m.ST_DRAFT: m.ST_PENDING, m.ST_REJECTED: m.ST_PENDING},
        "withdraw": {m.ST_PENDING: m.ST_DRAFT},
        "approve": {m.ST_PENDING: m.ST_PENDING},  # 最终状态由级数决定
        "reject": {m.ST_PENDING: m.ST_REJECTED},
        "pay": {m.ST_APPROVED: m.ST_PAID},
        "unpay": {m.ST_PAID: m.ST_APPROVED},
    }
    if action not in table or cur not in table[action]:
        raise HTTPException(400, f"状态「{cur}」不允许执行该操作")
    return cur, table[action][cur]


@router.post("/{oid}/submit", response_model=s.ReimbursementDetail)
def submit(
    oid: int,
    payload: s.ActionIn | None = None,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    sec.ensure_can_view(user, r)
    _ensure_owner_or_delegate(user, r)
    if not r.items:
        raise HTTPException(400, "报销单没有费用明细，无法提交")
    if not r.applicant_id:
        raise HTTPException(400, "请先选择申请人再提交")
    from_status, to_status = _transition(r, "submit")
    # 提交时按金额与当前阈值确定需要几级审批，并清零已通过级数
    r.required_level = ap.required_level(db, r.total_amount)
    r.approved_level = m.LEVEL_NONE
    r.status = to_status
    r.submit_at = datetime.now()
    r.reject_reason = None
    r.approver = None
    note = f"提交，需 {r.required_level} 级审批（金额 ¥{ser.money(r.total_amount):,.2f}）"
    if payload and payload.comment:
        note += f"；{payload.comment}"
    _log(db, r, "提交", user.name, note, from_status, to_status, level=1)
    db.commit()
    return ser.reimbursement_detail(_load(db, r.id))


@router.post("/{oid}/withdraw", response_model=s.ReimbursementDetail)
def withdraw(
    oid: int,
    payload: s.ActionIn | None = None,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    sec.ensure_can_view(user, r)
    _ensure_owner_or_delegate(user, r)
    from_status, to_status = _transition(r, "withdraw")
    r.status = to_status
    r.submit_at = None
    r.approved_level = m.LEVEL_NONE
    _log(db, r, "撤回", user.name, payload.comment if payload else None, from_status, to_status)
    db.commit()
    return ser.reimbursement_detail(_load(db, r.id))


@router.post("/{oid}/approve", response_model=s.ReimbursementDetail)
def approve(
    oid: int,
    payload: s.ActionIn | None = None,
    user: m.AppUser = Depends(sec.require_roles(m.ROLE_APPROVER, m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    sec.ensure_can_view(user, r)
    _transition(r, "approve")

    level = _next_pending_level(r)
    if user.role == m.ROLE_APPROVER:
        if not user.approval_level:
            raise HTTPException(403, "当前账号没有审批级别，无法审批")
        if user.approval_level < level:
            raise HTTPException(
                403, f"该单当前需要 {level} 级审批，你的审批级别为 {user.approval_level} 级"
            )
        # 审批人只能审本部门的单
        if r.department_id != user.department_id:
            raise HTTPException(403, "只能审批本部门的报销单")

    from_status = r.status
    r.approved_level = level
    r.approver = user.name
    r.approver_id = user.id
    r.approve_at = datetime.now()

    if r.approved_level >= max(r.required_level, 1):
        r.status = m.ST_APPROVED
        note = f"{level} 级审批通过，审批完成"
        if payload and payload.comment:
            note += f"；{payload.comment}"
        _log(db, r, "通过", user.name, note, from_status, m.ST_APPROVED, level=level)
    else:
        r.status = m.ST_PENDING
        note = f"{level} 级审批通过，转 {level + 1} 级审批"
        if payload and payload.comment:
            note += f"；{payload.comment}"
        _log(db, r, "通过", user.name, note, from_status, m.ST_PENDING, level=level)
    db.commit()
    return ser.reimbursement_detail(_load(db, r.id))


@router.post("/{oid}/reject", response_model=s.ReimbursementDetail)
def reject(
    oid: int,
    payload: s.ActionIn | None = None,
    user: m.AppUser = Depends(sec.require_roles(m.ROLE_APPROVER, m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    sec.ensure_can_view(user, r)
    if user.role == m.ROLE_APPROVER:
        if not user.approval_level:
            raise HTTPException(403, "当前账号没有审批级别，无法审批")
        if r.department_id != user.department_id:
            raise HTTPException(403, "只能审批本部门的报销单")
    if not (payload and payload.comment):
        raise HTTPException(400, "驳回必须填写原因")
    from_status, to_status = _transition(r, "reject")
    level = _next_pending_level(r)
    r.status = to_status
    r.reject_reason = payload.comment
    r.approver = user.name
    r.approver_id = user.id
    _log(db, r, "驳回", user.name, payload.comment, from_status, to_status, level=level)
    db.commit()
    return ser.reimbursement_detail(_load(db, r.id))


@router.post("/{oid}/pay", response_model=s.ReimbursementDetail)
def pay(
    oid: int,
    payload: s.ActionIn | None = None,
    user: m.AppUser = Depends(sec.require_roles(m.ROLE_FINANCE, m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    from_status, to_status = _transition(r, "pay")
    r.status = to_status
    r.pay_at = datetime.now()
    _log(db, r, "付款", user.name, payload.comment if payload else None, from_status, to_status)
    db.commit()
    return ser.reimbursement_detail(_load(db, r.id))


@router.post("/{oid}/unpay", response_model=s.ReimbursementDetail)
def unpay(
    oid: int,
    payload: s.ActionIn | None = None,
    user: m.AppUser = Depends(sec.require_roles(m.ROLE_FINANCE, m.ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    from_status, to_status = _transition(r, "unpay")
    r.status = to_status
    r.pay_at = None
    _log(db, r, "撤销付款", user.name, payload.comment if payload else None, from_status, to_status)
    db.commit()
    return ser.reimbursement_detail(_load(db, r.id))


@router.get("/{oid}/logs", response_model=list[s.ApprovalLogOut])
def get_logs(
    oid: int,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    r = _load(db, oid)
    sec.ensure_can_view(user, r)
    return [ser.log_out(lg) for lg in r.logs]
