"""主数据 CRUD：部门 / 员工 / 费用类型 / 客户 / 项目 / 预算。

注意：本模块刻意不使用 `from __future__ import annotations`。
register_crud 里路由函数把闭包变量 schema_in 直接当作参数注解，
若注解被延迟成字符串，FastAPI 无法解析该类型，会把 body 误判成 query 参数。
"""

import calendar
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models as m
from .. import schemas as s
from .. import security as sec
from .. import serializers as ser
from ..database import get_db

router = APIRouter(prefix="/api", tags=["主数据"])

# 读：任何已登录用户（前端下拉框都要用）
# 写：分两档——组织架构类只给管理员，费用口径与预算类财务也要能维护
_any_user = sec.current_user
_admin_only = sec.require_roles(m.ROLE_ADMIN)
_finance_or_admin = sec.require_roles(m.ROLE_FINANCE, m.ROLE_ADMIN)


# ------------------------------------------------------------------ 通用 CRUD
def _list(db, model, search_fields, q, serializer, extra_filter=None, ordering=None):
    query = db.query(model)
    if q and search_fields:
        cond = [getattr(model, f).like(f"%{q}%") for f in search_fields]
        query = query.filter(or_(*cond))
    if extra_filter is not None:
        query = query.filter(extra_filter)
    query = query.order_by(*(ordering or [model.id.desc()]))
    return query.all()


def register_crud(
    *,
    path: str,
    model,
    schema_in,
    serializer,
    search_fields: tuple[str, ...] = ("name",),
    unique_fields: tuple[str, ...] = (),
    ref_check=None,
    create_hook=None,
    ordering=None,
    write_dep=None,
):
    # 权限用 dependencies 声明，避免在闭包里直接依赖用户对象（FastAPI 需要能解析注解）
    write_guard = [Depends(write_dep)] if write_dep else []

    @router.get(f"/{path}", response_model=list[dict], dependencies=[Depends(_any_user)])
    def _get_all(
        q: str | None = Query(None, description="模糊搜索"),
        db: Session = Depends(get_db),
    ):
        return [serializer(o) for o in _list(db, model, search_fields, q, serializer, ordering=ordering)]

    @router.post(f"/{path}", response_model=dict, status_code=201, dependencies=write_guard)
    def _create(payload: schema_in, db: Session = Depends(get_db)):  # type: ignore[valid-type]
        data = payload.model_dump(exclude_unset=False)
        if create_hook:
            create_hook(db, data)
        obj = model(**data)
        db.add(obj)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(400, f"数据冲突（{'/'.join(unique_fields) or '唯一约束'} 已存在）：{exc.orig}")
        db.refresh(obj)
        return serializer(obj)

    @router.put(f"/{path}/{{oid}}", response_model=dict, dependencies=write_guard)
    def _update(oid: int, payload: schema_in, db: Session = Depends(get_db)):  # type: ignore[valid-type]
        obj = db.get(model, oid)
        if not obj:
            raise HTTPException(404, "记录不存在")
        for k, v in payload.model_dump(exclude_unset=False).items():
            setattr(obj, k, v)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(400, f"数据冲突：{exc.orig}")
        db.refresh(obj)
        return serializer(obj)

    @router.delete(f"/{path}/{{oid}}", dependencies=write_guard)
    def _delete(oid: int, db: Session = Depends(get_db)):
        obj = db.get(model, oid)
        if not obj:
            raise HTTPException(404, "记录不存在")
        if ref_check:
            msg = ref_check(db, oid)
            if msg:
                raise HTTPException(400, msg)
        db.delete(obj)
        db.commit()
        return {"ok": True, "deleted": oid}

    @router.post(f"/{path}/batch-delete", response_model=dict, dependencies=write_guard)
    def _batch_delete(payload: dict, db: Session = Depends(get_db)):
        """批量删除：被业务引用的记录逐条拦下并说明原因，不做「删一半留一半」的静默处理。"""
        ids = [int(i) for i in (payload.get("ids") or []) if str(i).isdigit()]
        if not ids:
            raise HTTPException(400, "请先勾选要删除的记录")
        if len(ids) > 500:
            raise HTTPException(400, "单次最多删除 500 条，请分批操作")
        deleted: list[int] = []
        skipped: list[dict] = []
        for oid in ids:
            obj = db.get(model, oid)
            if not obj:
                skipped.append({"id": oid, "reason": "记录不存在"})
                continue
            if ref_check:
                msg = ref_check(db, oid)
                if msg:
                    skipped.append({"id": oid, "name": getattr(obj, "name", None), "reason": msg})
                    continue
            db.delete(obj)
            db.flush()
            deleted.append(oid)
        db.commit()
        return {"ok": True, "deleted": len(deleted), "deleted_ids": deleted,
                "skipped": skipped, "skipped_count": len(skipped)}


# ------------------------------------------------------------------ 部门
def _dept_ref(db, oid):
    if db.query(m.Employee).filter(m.Employee.department_id == oid).count():
        return "该部门下仍有员工，无法删除"
    if db.query(m.Reimbursement).filter(m.Reimbursement.department_id == oid).count():
        return "该部门已被报销单引用，无法删除"
    if db.query(m.Budget).filter(m.Budget.department_id == oid).count():
        return "该部门已配置预算，无法删除"
    return None


register_crud(
    path="departments",
    model=m.Department,
    schema_in=s.DepartmentIn,
    serializer=ser.department_out,
    search_fields=("name", "code", "manager"),
    unique_fields=("name", "code"),
    ref_check=_dept_ref,
    ordering=[m.Department.id.asc()],
    write_dep=_admin_only,
)

# ------------------------------------------------------------------ 员工
def _emp_ref(db, oid):
    if db.query(m.Reimbursement).filter(m.Reimbursement.applicant_id == oid).count():
        return "该员工已有报销单记录，无法删除（可改为停用）"
    return None


@router.get("/employees", response_model=list[s.EmployeeOut], dependencies=[Depends(_any_user)])
def list_employees(
    q: str | None = None,
    department_id: int | None = None,
    active_only: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(m.Employee)
    if q:
        query = query.filter(
            or_(m.Employee.name.like(f"%{q}%"), m.Employee.employee_no.like(f"%{q}%"))
        )
    if department_id:
        query = query.filter(m.Employee.department_id == department_id)
    if active_only:
        query = query.filter(m.Employee.active.is_(True))
    return [ser.employee_out(o) for o in query.order_by(m.Employee.id.asc()).all()]


@router.post("/employees", response_model=s.EmployeeOut, status_code=201, dependencies=[Depends(_admin_only)])
def create_employee(payload: s.EmployeeIn, db: Session = Depends(get_db)):
    obj = m.Employee(**payload.model_dump())
    db.add(obj)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "工号已存在")
    db.refresh(obj)
    return ser.employee_out(obj)


@router.put("/employees/{oid}", response_model=s.EmployeeOut, dependencies=[Depends(_admin_only)])
def update_employee(oid: int, payload: s.EmployeeIn, db: Session = Depends(get_db)):
    obj = db.get(m.Employee, oid)
    if not obj:
        raise HTTPException(404, "记录不存在")
    for k, v in payload.model_dump().items():
        setattr(obj, k, v)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "工号已存在")
    db.refresh(obj)
    return ser.employee_out(obj)


@router.delete("/employees/{oid}", dependencies=[Depends(_admin_only)])
def delete_employee(oid: int, db: Session = Depends(get_db)):
    obj = db.get(m.Employee, oid)
    if not obj:
        raise HTTPException(404, "记录不存在")
    msg = _emp_ref(db, oid)
    if msg:
        raise HTTPException(400, msg)
    db.delete(obj)
    db.commit()
    return {"ok": True, "deleted": oid}


@router.post("/employees/batch-delete", response_model=dict, dependencies=[Depends(_admin_only)])
def batch_delete_employees(payload: dict, db: Session = Depends(get_db)):
    """批量删除员工。已被报销单引用的会跳过（建议改为停用），并带回原因。"""
    ids = [int(i) for i in (payload.get("ids") or []) if str(i).isdigit()]
    if not ids:
        raise HTTPException(400, "请先勾选要删除的员工")
    if len(ids) > 500:
        raise HTTPException(400, "单次最多删除 500 条，请分批操作")
    deleted: list[int] = []
    skipped: list[dict] = []
    for oid in ids:
        obj = db.get(m.Employee, oid)
        if not obj:
            skipped.append({"id": oid, "reason": "记录不存在"})
            continue
        if db.query(m.AppUser).filter(m.AppUser.employee_id == oid).count():
            skipped.append({"id": oid, "name": obj.name, "reason": "该员工绑定了登录账号，请先解绑"})
            continue
        msg = _emp_ref(db, oid)
        if msg:
            skipped.append({"id": oid, "name": obj.name, "reason": msg})
            continue
        db.delete(obj)
        db.flush()
        deleted.append(oid)
    db.commit()
    return {"ok": True, "deleted": len(deleted), "deleted_ids": deleted,
            "skipped": skipped, "skipped_count": len(skipped)}


# ------------------------------------------------------------------ 费用类型
def _cat_ref(db, oid):
    if db.query(m.ReimbursementItem).filter(m.ReimbursementItem.category_id == oid).count():
        return "该费用类型已被报销明细引用，无法删除（可改为停用）"
    if db.query(m.Invoice).filter(m.Invoice.category_id == oid).count():
        return "该费用类型已被发票引用，无法删除"
    return None


register_crud(
    path="categories",
    model=m.ExpenseCategory,
    schema_in=s.CategoryIn,
    serializer=ser.category_out,
    search_fields=("name", "code", "group_name"),
    unique_fields=("name", "code"),
    ref_check=_cat_ref,
    ordering=[m.ExpenseCategory.id.asc()],
    write_dep=_finance_or_admin,
)

# ------------------------------------------------------------------ 客户
register_crud(
    path="customers",
    model=m.Customer,
    schema_in=s.CustomerIn,
    serializer=ser.customer_out,
    search_fields=("name", "code", "contact"),
    unique_fields=("name", "code"),
    ordering=[m.Customer.id.asc()],
    write_dep=_admin_only,
)

# ------------------------------------------------------------------ 项目
def _proj_ref(db, oid):
    if db.query(m.Reimbursement).filter(m.Reimbursement.project_id == oid).count():
        return "该项目已被报销单引用，无法删除"
    if db.query(m.Budget).filter(m.Budget.project_id == oid).count():
        return "该项目已配置预算，无法删除"
    return None


register_crud(
    path="projects",
    model=m.Project,
    schema_in=s.ProjectIn,
    serializer=ser.project_out,
    search_fields=("name", "code", "manager"),
    unique_fields=("name", "code"),
    ref_check=_proj_ref,
    ordering=[m.Project.id.asc()],
    write_dep=_admin_only,
)


# ------------------------------------------------------------------ 预算
def _budget_window(year: int, month: int | None) -> tuple[date, date]:
    if month:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last)
    return date(year, 1, 1), date(year, 12, 31)


def budget_used(db: Session, b: m.Budget) -> float:
    """已用额度 = 落在预算周期内、状态为已通过/已付款的报销明细合计。"""
    start, end = _budget_window(b.year, b.month)
    q = (
        db.query(func.coalesce(func.sum(m.ReimbursementItem.amount), 0))
        .join(m.Reimbursement, m.ReimbursementItem.reimbursement_id == m.Reimbursement.id)
        .filter(m.Reimbursement.status.in_([m.ST_APPROVED, m.ST_PAID]))
        .filter(m.ReimbursementItem.occur_date >= start)
        .filter(m.ReimbursementItem.occur_date <= end)
    )
    if b.department_id:
        q = q.filter(m.Reimbursement.department_id == b.department_id)
    if b.project_id:
        q = q.filter(m.Reimbursement.project_id == b.project_id)
    if b.category_id:
        q = q.filter(m.ReimbursementItem.category_id == b.category_id)
    return ser.money(q.scalar())


@router.get("/budgets", response_model=list[s.BudgetOut], dependencies=[Depends(_finance_or_admin)])
def list_budgets(
    year: int | None = None,
    month: int | None = None,
    department_id: int | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(m.Budget)
    if year:
        query = query.filter(m.Budget.year == year)
    if month is not None:
        query = query.filter(or_(m.Budget.month == month, m.Budget.month.is_(None)))
    if department_id:
        query = query.filter(m.Budget.department_id == department_id)
    rows = query.order_by(m.Budget.year.desc(), m.Budget.month.asc().nullsfirst()).all()
    return [ser.budget_out(b, budget_used(db, b)) for b in rows]


@router.post("/budgets", response_model=s.BudgetOut, status_code=201, dependencies=[Depends(_finance_or_admin)])
def create_budget(payload: s.BudgetIn, db: Session = Depends(get_db)):
    data = payload.model_dump()
    dup = (
        db.query(m.Budget)
        .filter(
            m.Budget.year == data["year"],
            m.Budget.month.is_(None) if data["month"] is None else m.Budget.month == data["month"],
            m.Budget.department_id.is_(None)
            if data["department_id"] is None
            else m.Budget.department_id == data["department_id"],
            m.Budget.project_id.is_(None)
            if data["project_id"] is None
            else m.Budget.project_id == data["project_id"],
            m.Budget.category_id.is_(None)
            if data["category_id"] is None
            else m.Budget.category_id == data["category_id"],
        )
        .first()
    )
    if dup:
        raise HTTPException(400, "该维度组合的预算已存在，请直接编辑")
    obj = m.Budget(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return ser.budget_out(obj, budget_used(db, obj))


@router.put("/budgets/{oid}", response_model=s.BudgetOut, dependencies=[Depends(_finance_or_admin)])
def update_budget(oid: int, payload: s.BudgetIn, db: Session = Depends(get_db)):
    obj = db.get(m.Budget, oid)
    if not obj:
        raise HTTPException(404, "记录不存在")
    for k, v in payload.model_dump().items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return ser.budget_out(obj, budget_used(db, obj))


@router.delete("/budgets/{oid}", dependencies=[Depends(_finance_or_admin)])
def delete_budget(oid: int, db: Session = Depends(get_db)):
    obj = db.get(m.Budget, oid)
    if not obj:
        raise HTTPException(404, "记录不存在")
    db.delete(obj)
    db.commit()
    return {"ok": True, "deleted": oid}


# ------------------------------------------------------------------ 费用明细台账
@router.get("/items", response_model=s.Page)
def list_items(
    q: str | None = None,
    category_id: int | None = None,
    department_id: int | None = None,
    customer_id: int | None = None,
    project_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "date_desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: m.AppUser = Depends(_any_user),
    db: Session = Depends(get_db),
):
    """费用明细台账：跨报销单的费用行视角。"""
    from sqlalchemy.orm import selectinload

    conds = list(sec.scope_conds(user))
    if q:
        like = f"%{q}%"
        conds.append(
            or_(
                m.ReimbursementItem.description.like(like),
                m.Reimbursement.code.like(like),
                m.Reimbursement.title.like(like),
            )
        )
    if category_id:
        conds.append(m.ReimbursementItem.category_id == category_id)
    if department_id:
        conds.append(m.Reimbursement.department_id == department_id)
    if customer_id:
        conds.append(m.Reimbursement.customer_id == customer_id)
    if project_id:
        conds.append(m.Reimbursement.project_id == project_id)
    if status:
        conds.append(m.Reimbursement.status.in_(status.split(",")))
    if date_from:
        conds.append(m.ReimbursementItem.occur_date >= date_from)
    if date_to:
        conds.append(m.ReimbursementItem.occur_date <= date_to)

    def joined(entities):
        stmt = entities.select_from(m.ReimbursementItem).join(
            m.Reimbursement, m.ReimbursementItem.reimbursement_id == m.Reimbursement.id
        )
        return stmt.where(*conds) if conds else stmt

    agg = db.execute(
        joined(
            select(
                func.count(m.ReimbursementItem.id),
                func.coalesce(func.sum(m.ReimbursementItem.amount), 0),
            )
        )
    ).one()
    total, total_amount = agg[0], ser.money(agg[1])

    order = {
        "date_desc": m.ReimbursementItem.occur_date.desc().nullslast(),
        "date_asc": m.ReimbursementItem.occur_date.asc().nullslast(),
        "amount_desc": m.ReimbursementItem.amount.desc(),
        "amount_asc": m.ReimbursementItem.amount.asc(),
        "id_desc": m.ReimbursementItem.id.desc(),
    }.get(sort, m.ReimbursementItem.occur_date.desc().nullslast())

    query = (
        db.query(m.ReimbursementItem)
        .join(m.Reimbursement, m.ReimbursementItem.reimbursement_id == m.Reimbursement.id)
        .filter(*conds)
        .options(
            selectinload(m.ReimbursementItem.category),
            selectinload(m.ReimbursementItem.invoices),
            selectinload(m.ReimbursementItem.reimbursement).selectinload(m.Reimbursement.applicant),
            selectinload(m.ReimbursementItem.reimbursement).selectinload(m.Reimbursement.department),
            selectinload(m.ReimbursementItem.reimbursement).selectinload(m.Reimbursement.customer),
        )
    )
    rows = query.order_by(order).offset((page - 1) * page_size).limit(page_size).all()
    items = []
    for it in rows:
        r = it.reimbursement
        d = ser.item_out(it)
        d.update(
            {
                "reimbursement_id": r.id if r else None,
                "reimbursement_code": r.code if r else None,
                "reimbursement_title": r.title if r else None,
                "reimbursement_status": r.status if r else None,
                "applicant_name": r.applicant.name if r and r.applicant else None,
                "department_name": r.department.name if r and r.department else None,
                "customer_name": r.customer.name if r and r.customer else None,
            }
        )
        items.append(d)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_amount": ser.money(total_amount),
        "items": items,
    }


# ------------------------------------------------------------------ 元信息
@router.get("/meta", response_model=dict, dependencies=[Depends(_any_user)])
def meta(db: Session = Depends(get_db)):
    """前端启动时一次性拉取全部字典数据。"""
    return {
        "departments": [ser.department_out(o) for o in db.query(m.Department).order_by(m.Department.id).all()],
        "employees": [
            ser.employee_out(o) for o in db.query(m.Employee).order_by(m.Employee.id).all()
        ],
        "categories": [
            ser.category_out(o) for o in db.query(m.ExpenseCategory).order_by(m.ExpenseCategory.id).all()
        ],
        "customers": [ser.customer_out(o) for o in db.query(m.Customer).order_by(m.Customer.id).all()],
        "projects": [ser.project_out(o) for o in db.query(m.Project).order_by(m.Project.id).all()],
        "statuses": m.STATUS_FLOW,
        "invoice_check_statuses": [m.CHECK_UNVERIFIED, m.CHECK_OK, m.CHECK_BAD],
        "invoice_types": [
            "增值税专用发票",
            "增值税普通发票",
            "增值税电子普通发票",
            "电子发票（普通发票）",
            "电子发票（增值税专用发票）",
            "定额发票",
            "火车票",
            "航空运输电子客票行程单",
            "出租车票",
            "其他",
        ],
    }
