"""统计看板与异常预警聚合接口。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import calendar

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from .. import security as sec
from .. import serializers as ser
from ..database import get_db

router = APIRouter(prefix="/api/stats", tags=["统计看板"])

# 组织级分析视图（预算执行、异常预警）只对管理与复核角色开放。
_ANALYST = sec.require_roles(m.ROLE_APPROVER, m.ROLE_FINANCE, m.ROLE_ADMIN)


# ------------------------------------------------------------------ 工具
def _d(v) -> date | None:
    """把 'YYYY-MM-DD' / date 统一成 date，避免跨库隐式转换差异。"""
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _shift_month(key: str, delta: int) -> str:
    total = int(key[:4]) * 12 + (int(key[5:7]) - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _rq(db, date_from=None, date_to=None, department_id=None, customer_id=None, project_id=None,
        scope=None):
    """报销单维度基础查询。scope 是登录用户的数据可见范围，必须最先应用。"""
    q = db.query(m.Reimbursement)
    if scope:
        q = q.filter(*scope)
    if _d(date_from):
        q = q.filter(m.Reimbursement.occur_start >= _d(date_from))
    if _d(date_to):
        q = q.filter(m.Reimbursement.occur_end <= _d(date_to))
    if department_id:
        q = q.filter(m.Reimbursement.department_id == department_id)
    if customer_id:
        q = q.filter(m.Reimbursement.customer_id == customer_id)
    if project_id:
        q = q.filter(m.Reimbursement.project_id == project_id)
    return q


def _scoped(stmt, scope):
    """给带 Reimbursement 关联的 select 语句套上数据范围。"""
    return stmt.where(*scope) if scope else stmt



# ------------------------------------------------------------------ 概览
@router.get("/overview", response_model=dict)
def overview(
    date_from: str | None = None,
    date_to: str | None = None,
    department_id: int | None = None,
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    today = date.today()
    this_month = today.strftime("%Y-%m")
    prev_key = _shift_month(this_month, -1)
    # 本月尚未结束，环比必须与上月「同期」比，否则会得出误导性的巨幅下滑
    prev_y, prev_m = int(prev_key[:4]), int(prev_key[5:7])
    prev_cut = min(today.day, calendar.monthrange(prev_y, prev_m)[1])
    rq = _rq(db, date_from, date_to, department_id, scope=scope)

    rows = rq.with_entities(
        m.Reimbursement.occur_start, m.Reimbursement.total_amount, m.Reimbursement.status
    ).all()

    total_amount = paid_amount = pending_amount = month_amount = 0.0
    prev_amount = prev_amount_full = 0.0
    month_count = 0
    status_amount: dict[str, float] = defaultdict(float)
    status_count: dict[str, int] = defaultdict(int)

    for occur, amount, status in rows:
        amt = ser.money(amount)
        total_amount += amt
        status_amount[status] += amt
        status_count[status] += 1
        if status == m.ST_PAID:
            paid_amount += amt
        elif status == m.ST_PENDING:
            pending_amount += amt
        if occur:
            key = _month_key(occur)
            if key == this_month:
                month_amount += amt
                month_count += 1
            elif key == prev_key:
                prev_amount_full += amt
                if occur.day <= prev_cut:
                    prev_amount += amt

    inv_stmt = select(
        func.count(m.Invoice.id), func.coalesce(func.sum(m.Invoice.amount), 0)
    )
    if scope:
        # 非全局角色只能看到「权限范围内报销单所关联的发票」。
        # 未关联的散票不计入，否则可以靠发票金额侧漏出全公司数据。
        inv_stmt = inv_stmt.select_from(m.Invoice).join(
            m.Reimbursement, m.Invoice.reimbursement_id == m.Reimbursement.id
        )
        inv_stmt = _scoped(inv_stmt, scope)
    if _d(date_from):
        inv_stmt = inv_stmt.where(m.Invoice.invoice_date >= _d(date_from))
    if _d(date_to):
        inv_stmt = inv_stmt.where(m.Invoice.invoice_date <= _d(date_to))
    inv_count, inv_amount = db.execute(inv_stmt).one()

    pending_cnt = rq.filter(m.Reimbursement.status == m.ST_PENDING).count()
    overdue = (
        rq.filter(m.Reimbursement.status == m.ST_PENDING)
        .filter(m.Reimbursement.submit_at <= datetime.now() - timedelta(days=7))
        .count()
    )

    return {
        "total_amount": ser.money(total_amount),
        "total_count": len(rows),
        "avg_amount": ser.money(total_amount / len(rows)) if rows else 0.0,
        "paid_amount": ser.money(paid_amount),
        "pending_amount": ser.money(pending_amount),
        "pending_count": pending_cnt,
        "overdue_count": overdue,
        "month_amount": ser.money(month_amount),
        "month_count": month_count,
        "prev_month_amount": ser.money(prev_amount),
        "prev_month_amount_full": ser.money(prev_amount_full),
        "mom": round((month_amount - prev_amount) / prev_amount * 100, 1) if prev_amount else None,
        "mom_basis": f"{today.month}月1-{today.day}日 对比 {prev_m}月1-{prev_cut}日",
        "invoice_count": inv_count or 0,
        "invoice_amount": ser.money(inv_amount),
        "status_dist": sorted(
            [
                {"name": k, "amount": ser.money(v), "count": status_count[k]}
                for k, v in status_amount.items()
            ],
            key=lambda x: -x["amount"],
        ),
    }


# ------------------------------------------------------------------ 趋势
@router.get("/trend", response_model=list[dict])
def trend(
    months: int = Query(12, ge=1, le=36),
    date_from: str | None = None,
    date_to: str | None = None,
    department_id: int | None = None,
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    rows = _rq(db, date_from, date_to, department_id, scope=scope).with_entities(
        m.Reimbursement.occur_start, m.Reimbursement.total_amount, m.Reimbursement.status
    ).all()

    start_key = _shift_month(date.today().strftime("%Y-%m"), -(months - 1))
    buckets = {
        _shift_month(start_key, i): {"month": _shift_month(start_key, i), "amount": 0.0,
                                     "count": 0, "approved_amount": 0.0}
        for i in range(months)
    }
    for occur, amount, status in rows:
        if not occur:
            continue
        k = _month_key(occur)
        if k not in buckets:
            continue
        amt = ser.money(amount)
        buckets[k]["amount"] += amt
        buckets[k]["count"] += 1
        if status in (m.ST_APPROVED, m.ST_PAID):
            buckets[k]["approved_amount"] += amt

    return [
        {
            "month": k,
            "amount": ser.money(buckets[k]["amount"]),
            "approved_amount": ser.money(buckets[k]["approved_amount"]),
            "count": buckets[k]["count"],
        }
        for k in sorted(buckets)
    ]


# ------------------------------------------------------------------ 费用结构
def _items_by(db, dim_table, dim_id, dim_name, join_cond, date_from, date_to, department_id,
              status_filter, scope=None):
    """按维度汇总费用明细。

    注意 join_cond 必须显式传入（明细表 -> 维度表的外键条件）。
    若用 `dim_id == dim_table.id` 生成条件，当维度就是被 group 的表时会退化成恒真，
    在 SQLite 下会产生笛卡尔积，让每个分组都统计到全量数据。
    """
    stmt = (
        select(
            dim_id,
            dim_name,
            func.coalesce(func.sum(m.ReimbursementItem.amount), 0),
            func.count(m.ReimbursementItem.id),
        )
        .select_from(m.ReimbursementItem)
        .join(m.Reimbursement, m.ReimbursementItem.reimbursement_id == m.Reimbursement.id)
        .join(dim_table, join_cond, isouter=True)
        .group_by(dim_id, dim_name)
    )
    stmt = _scoped(stmt, scope)
    if _d(date_from):
        stmt = stmt.where(m.ReimbursementItem.occur_date >= _d(date_from))
    if _d(date_to):
        stmt = stmt.where(m.ReimbursementItem.occur_date <= _d(date_to))
    if department_id:
        stmt = stmt.where(m.Reimbursement.department_id == department_id)
    if status_filter:
        stmt = stmt.where(m.Reimbursement.status.in_(status_filter.split(",")))
    rows = db.execute(stmt).all()
    total = sum(ser.money(r[2]) for r in rows) or 1
    return sorted(
        [
            {
                "id": r[0],
                "name": r[1] or "未分类",
                "amount": ser.money(r[2]),
                "count": r[3],
                "ratio": round(ser.money(r[2]) / total * 100, 1),
            }
            for r in rows
        ],
        key=lambda x: -x["amount"],
    )


@router.get("/by-category", response_model=list[dict])
def by_category(
    date_from: str | None = None,
    date_to: str | None = None,
    department_id: int | None = None,
    status_filter: str | None = None,
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    return _items_by(
        db,
        m.ExpenseCategory,
        m.ExpenseCategory.id,
        m.ExpenseCategory.name,
        m.ReimbursementItem.category_id == m.ExpenseCategory.id,
        date_from,
        date_to,
        department_id,
        status_filter,
        scope,
    )


@router.get("/by-group", response_model=list[dict])
def by_group(
    date_from: str | None = None,
    date_to: str | None = None,
    department_id: int | None = None,
    status_filter: str | None = None,
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    """按费用大类（一级科目）汇总。"""
    stmt = (
        select(
            m.ExpenseCategory.group_name,
            func.coalesce(func.sum(m.ReimbursementItem.amount), 0),
            func.count(m.ReimbursementItem.id),
        )
        .select_from(m.ReimbursementItem)
        .join(m.Reimbursement, m.ReimbursementItem.reimbursement_id == m.Reimbursement.id)
        .join(m.ExpenseCategory, m.ReimbursementItem.category_id == m.ExpenseCategory.id, isouter=True)
        .group_by(m.ExpenseCategory.group_name)
    )
    stmt = _scoped(stmt, scope)
    if _d(date_from):
        stmt = stmt.where(m.ReimbursementItem.occur_date >= _d(date_from))
    if _d(date_to):
        stmt = stmt.where(m.ReimbursementItem.occur_date <= _d(date_to))
    if department_id:
        stmt = stmt.where(m.Reimbursement.department_id == department_id)
    if status_filter:
        stmt = stmt.where(m.Reimbursement.status.in_(status_filter.split(",")))
    rows = db.execute(stmt).all()
    total = sum(ser.money(r[1]) for r in rows) or 1
    return sorted(
        [
            {
                "name": r[0] or "其他",
                "amount": ser.money(r[1]),
                "count": r[2],
                "ratio": round(ser.money(r[1]) / total * 100, 1),
            }
            for r in rows
        ],
        key=lambda x: -x["amount"],
    )


# ------------------------------------------------------------------ 维度排名
def _by_dim(db, dim_id_col, dim_table, dim_name_col, date_from, date_to, limit=0, scope=None):
    stmt = (
        select(
            dim_id_col,
            dim_name_col,
            func.coalesce(func.sum(m.Reimbursement.total_amount), 0),
            func.count(m.Reimbursement.id),
        )
        .select_from(m.Reimbursement)
        .join(dim_table, dim_id_col == dim_table.id, isouter=True)
        .group_by(dim_id_col, dim_name_col)
    )
    stmt = _scoped(stmt, scope)
    if _d(date_from):
        stmt = stmt.where(m.Reimbursement.occur_start >= _d(date_from))
    if _d(date_to):
        stmt = stmt.where(m.Reimbursement.occur_end <= _d(date_to))
    rows = db.execute(stmt).all()
    total = sum(ser.money(r[2]) for r in rows) or 1
    out = sorted(
        [
            {
                "id": r[0],
                "name": r[1] or "未指定",
                "amount": ser.money(r[2]),
                "count": r[3],
                "ratio": round(ser.money(r[2]) / total * 100, 1),
            }
            for r in rows
        ],
        key=lambda x: -x["amount"],
    )
    return out[:limit] if limit else out


@router.get("/by-department", response_model=list[dict])
def by_department(
    date_from: str | None = None,
    date_to: str | None = None,
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    return _by_dim(db, m.Reimbursement.department_id, m.Department, m.Department.name, date_from, date_to,
                   scope=scope)


@router.get("/by-employee", response_model=list[dict])
def by_employee(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(15, ge=1, le=100),
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    return _by_dim(db, m.Reimbursement.applicant_id, m.Employee, m.Employee.name, date_from, date_to, limit,
                   scope=scope)


@router.get("/by-customer", response_model=list[dict])
def by_customer(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(15, ge=1, le=100),
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    data = _by_dim(db, m.Reimbursement.customer_id, m.Customer, m.Customer.name, date_from, date_to, limit,
                   scope=scope)
    for row in data:
        row["paid_amount"] = 0.0
        if row["id"]:
            paid_stmt = (
                select(func.coalesce(func.sum(m.Reimbursement.total_amount), 0))
                .select_from(m.Reimbursement)
                .where(m.Reimbursement.customer_id == row["id"])
                .where(m.Reimbursement.status == m.ST_PAID)
            )
            paid = db.execute(_scoped(paid_stmt, scope)).scalar()
            row["paid_amount"] = ser.money(paid)
    return data


@router.get("/by-project", response_model=list[dict])
def by_project(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(15, ge=1, le=100),
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    return _by_dim(db, m.Reimbursement.project_id, m.Project, m.Project.name, date_from, date_to, limit,
                   scope=scope)


# ------------------------------------------------------------------ 多维堆叠
@router.get("/by-month-department", response_model=dict)
def by_month_department(
    months: int = Query(6, ge=1, le=24),
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    start_key = _shift_month(date.today().strftime("%Y-%m"), -(months - 1))
    start_date = date(int(start_key[:4]), int(start_key[5:7]), 1)

    stmt = (
        select(m.Department.name, m.Reimbursement.occur_start, m.Reimbursement.total_amount)
        .select_from(m.Reimbursement)
        .join(m.Department, m.Reimbursement.department_id == m.Department.id, isouter=True)
        .where(m.Reimbursement.occur_start >= start_date)
    )
    rows = db.execute(_scoped(stmt, scope)).all()

    months_list = [_shift_month(start_key, i) for i in range(months)]
    grid: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for dept, occur, amount in rows:
        if not occur:
            continue
        grid[dept or "未指定"][_month_key(occur)] += ser.money(amount)

    return {
        "months": months_list,
        "series": [
            {"name": d, "data": [ser.money(grid[d].get(mk, 0)) for mk in months_list]}
            for d in sorted(grid, key=lambda x: -sum(grid[x].values()))
        ],
    }


@router.get("/category-trend", response_model=dict)
def category_trend(
    months: int = Query(6, ge=1, le=24),
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    start_key = _shift_month(date.today().strftime("%Y-%m"), -(months - 1))
    start_date = date(int(start_key[:4]), int(start_key[5:7]), 1)

    stmt = (
        select(m.ExpenseCategory.name, m.ReimbursementItem.occur_date, m.ReimbursementItem.amount)
        .select_from(m.ReimbursementItem)
        # 明细表没有部门字段，必须 join 报销单才能套数据范围
        .join(m.Reimbursement, m.ReimbursementItem.reimbursement_id == m.Reimbursement.id)
        .join(m.ExpenseCategory, m.ReimbursementItem.category_id == m.ExpenseCategory.id, isouter=True)
        .where(m.ReimbursementItem.occur_date >= start_date)
    )
    rows = db.execute(_scoped(stmt, scope)).all()

    months_list = [_shift_month(start_key, i) for i in range(months)]
    grid: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for name, occur, amount in rows:
        if not occur:
            continue
        grid[name or "未分类"][_month_key(occur)] += ser.money(amount)

    return {
        "months": months_list,
        "series": [
            {"name": n, "data": [ser.money(grid[n].get(mk, 0)) for mk in months_list]}
            for n in sorted(grid, key=lambda x: -sum(grid[x].values()))
        ],
    }


# ------------------------------------------------------------------ 预算 / 预警
@router.get("/budget-execution", response_model=list[dict])
def budget_execution(
    year: int | None = None,
    _analyst: m.AppUser = Depends(_ANALYST),
    db: Session = Depends(get_db),
):
    from .master import budget_used

    year = year or date.today().year
    rows = (
        db.query(m.Budget)
        .filter(m.Budget.year == year)
        .order_by(m.Budget.month.asc().nullsfirst(), m.Budget.id.asc())
        .all()
    )
    return [ser.budget_out(b, budget_used(db, b)) for b in rows]


@router.get("/alerts", response_model=dict)
def alerts(
    large_amount: float = Query(20000, description="大额单阈值"),
    overdue_days: int = Query(7, description="待审批超期天数"),
    _analyst: m.AppUser = Depends(_ANALYST),
    scope: list = Depends(sec.current_scope),
    db: Session = Depends(get_db),
):
    """异常预警：超限额、缺发票、问题发票、大额单、超期未审批。"""
    cats = {c.id: c for c in db.query(m.ExpenseCategory).all()}

    over_limit, missing_invoice = [], []
    item_q = db.query(m.ReimbursementItem).options(
        selectinload(m.ReimbursementItem.invoices),
        selectinload(m.ReimbursementItem.category),
        selectinload(m.ReimbursementItem.reimbursement),
    )
    if scope:
        # 明细没有部门字段，需要 join 报销单才能按数据范围过滤
        item_q = item_q.join(
            m.Reimbursement, m.ReimbursementItem.reimbursement_id == m.Reimbursement.id
        ).filter(*scope)
    items = item_q.all()
    for it in items:
        r = it.reimbursement
        if not r:
            continue
        amt = ser.money(it.amount)
        cat = cats.get(it.category_id or -1)
        if cat and cat.single_limit and amt > ser.money(cat.single_limit):
            over_limit.append({
                "reimbursement_code": r.code,
                "reimbursement_id": r.id,
                "category": cat.name,
                "amount": amt,
                "limit": ser.money(cat.single_limit),
                "excess": ser.money(amt - ser.money(cat.single_limit)),
                "occur_date": it.occur_date.isoformat() if it.occur_date else None,
                "applicant": r.applicant.name if r.applicant else None,
            })
        if cat and cat.requires_invoice and not it.invoices:
            missing_invoice.append({
                "reimbursement_code": r.code,
                "reimbursement_id": r.id,
                "category": cat.name,
                "amount": amt,
                "description": it.description,
                "occur_date": it.occur_date.isoformat() if it.occur_date else None,
            })

    bad_q = db.query(m.Invoice).options(
        selectinload(m.Invoice.category), selectinload(m.Invoice.reimbursement)
    ).filter(m.Invoice.check_status == m.CHECK_BAD)
    if scope:
        bad_q = bad_q.join(
            m.Reimbursement, m.Invoice.reimbursement_id == m.Reimbursement.id
        ).filter(*scope)
    bad_invoices = bad_q.limit(50).all()

    large_q = (
        db.query(m.Reimbursement)
        .options(selectinload(m.Reimbursement.applicant), selectinload(m.Reimbursement.department))
        .filter(m.Reimbursement.total_amount >= large_amount)
        .order_by(m.Reimbursement.total_amount.desc())
    )
    if scope:
        large_q = large_q.filter(*scope)
    large = large_q.limit(30).all()

    cutoff = datetime.now() - timedelta(days=overdue_days)
    overdue_q = (
        db.query(m.Reimbursement)
        .options(selectinload(m.Reimbursement.applicant))
        .filter(m.Reimbursement.status == m.ST_PENDING)
        .filter(m.Reimbursement.submit_at <= cutoff)
        .order_by(m.Reimbursement.submit_at.asc())
    )
    if scope:
        overdue_q = overdue_q.filter(*scope)
    overdue = overdue_q.all()

    return {
        "summary": {
            "over_limit_count": len(over_limit),
            "missing_invoice_count": len(missing_invoice),
            "bad_invoice_count": len(bad_invoices),
            "large_amount_count": len(large),
            "overdue_count": len(overdue),
        },
        "over_limit": sorted(over_limit, key=lambda x: -x["excess"])[:30],
        "missing_invoice": sorted(missing_invoice, key=lambda x: -x["amount"])[:30],
        "bad_invoices": [ser.invoice_out(v) for v in bad_invoices],
        "large_amount": [
            {
                "id": r.id,
                "code": r.code,
                "title": r.title,
                "amount": ser.money(r.total_amount),
                "status": r.status,
                "applicant": r.applicant.name if r.applicant else None,
                "department": r.department.name if r.department else None,
                "occur_start": r.occur_start.isoformat() if r.occur_start else None,
            }
            for r in large
        ],
        "overdue": [
            {
                "id": r.id,
                "code": r.code,
                "title": r.title,
                "amount": ser.money(r.total_amount),
                "applicant": r.applicant.name if r.applicant else None,
                "submit_at": ser.dt_s(r.submit_at),
                "days": (datetime.now() - r.submit_at).days if r.submit_at else None,
                "pending_level": min((r.approved_level or 0) + 1, max(r.required_level or 1, 1)),
            }
            for r in overdue
        ],
    }
