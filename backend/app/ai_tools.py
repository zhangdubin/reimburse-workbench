"""助手能力层：把整套系统的读/写能力交给大模型，但不绕过既有接口。

三层
----
1. **只读工具**（`READ_TOOLS`）——服务端直接查库并回一份紧凑结果，模型可以连着调几轮。
   早先助手只能看到 prompt 里预先塞好的那点数据，于是「有哪些异常发票」答不上来、
   「客户 XTS 存不存在」也看不见（其实 id=13 早就在库里）。有了工具，它就能自己去查。
2. **写动作**（`WRITE_ACTIONS`）——不直接写库，产出结构化方案交前端渲染确认卡片，
   用户点确认后由前端带着**用户自己的会话**调用既有业务接口。权限校验、状态机、
   审计日志因此仍然只有一套实现。
3. **参数预校验**（每个动作的 `prepare`）——在把卡片端到人眼前之前，先把参数规范化：
   名称→id 解析、缺省值补全、必填校验、状态可编辑性校验。教训来自实测：
   助手给了张「新建报销单」的卡片，用户点确认后才报「执行失败：请指定申请人」——
   校验发生在用户点完确认之后，太晚了，而且卡片上的参数本来就是程序生成的，
   缺什么应该当场补上或当场问清楚。

为什么不在后端直接执行写操作
----------------------------
财务场景要可追溯：谁点的确认、改了什么，必须在审计里留痕，且与人工操作完全同源。
让前端拿用户的会话去调既有接口，等于「AI 只负责把参数准备好」，最坏情况是建议得不对，
不会出现「绕过校验改了数据」。

刻意不做的事
------------
不提供「清空全部」「批量清库」这类动作。它们是运维动作，需要人工在界面上输入确认语，
不应该由一句话触达。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from . import models as m
from . import security as sec
from . import serializers as ser
from . import jev_client

ALL_ROLES: tuple[str, ...] = tuple(m.ROLES)
STAFF: tuple[str, ...] = (m.ROLE_FINANCE, m.ROLE_ADMIN)
ADMIN_ONLY: tuple[str, ...] = (m.ROLE_ADMIN,)
APPROVERS: tuple[str, ...] = (m.ROLE_APPROVER, m.ROLE_ADMIN)


class ToolError(RuntimeError):
    """工具/动作校验失败。message 是一句可以直接给用户看的中文。"""


@dataclass
class Ctx:
    """一次工具调用的执行环境。"""

    db: Session
    user: m.AppUser

    @property
    def scope(self) -> list:
        return sec.scope_conds(self.user)

    @property
    def invoice_scope(self) -> list:
        return sec.invoice_scope_conds(self.user)

    def can_role(self, roles) -> bool:
        return self.user.role in roles


# ================================================================== 通用小工具


def _limit_of(value, default: int = 20, cap: int = 100) -> int:
    """把模型给的条数收进安全区间。模型很爱给 1000。"""
    n = _as_int(value)
    return max(1, min(n or default, cap))


def _as_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(",", "").replace("元", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def _as_date(value, default: date | None = None) -> date | None:
    if value in (None, ""):
        return default
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-").replace(".", "-")
    if text in ("今天", "今日"):
        return date.today()
    if text in ("昨天", "昨日"):
        return date.today() - timedelta(days=1)
    if text in ("前天", "前日"):
        return date.today() - timedelta(days=2)
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        except ValueError:
            return default
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return default


def _period(params: dict) -> tuple[str | None, str | None, str]:
    """把「本月 / 上月 / 本年 / 近 N 月 / 具体日期」统一成 (date_from, date_to, 人话)。"""
    key = str(params.get("period") or "").strip()
    today = date.today()
    first = today.replace(day=1)
    if params.get("date_from") or params.get("date_to"):
        start = _as_date(params.get("date_from"))
        end = _as_date(params.get("date_to"))
        return (
            start.isoformat() if start else None,
            end.isoformat() if end else None,
            f"{start.isoformat() if start else '最早'} ~ {end.isoformat() if end else '今天'}",
        )

    def _shift(d: date, months: int) -> date:
        total = d.year * 12 + (d.month - 1) + months
        return date(total // 12, total % 12 + 1, 1)

    if key in ("本月", "这个月", "当月", ""):
        return first.isoformat(), today.isoformat(), "本月"
    if key in ("上月", "上个月"):
        prev_first = _shift(first, -1)
        prev_last = first - timedelta(days=1)
        return prev_first.isoformat(), prev_last.isoformat(), "上月"
    if key in ("本年", "今年", "全年"):
        return date(today.year, 1, 1).isoformat(), today.isoformat(), "本年"
    if key in ("全部", "所有", "不限"):
        return None, None, "全部时间"
    if key.startswith("近") and key.endswith("月"):
        n = _as_int(key[1:-1]) or 3
        return _shift(first, -(n - 1)).isoformat(), today.isoformat(), key
    if key.startswith("近") and key.endswith("天"):
        n = _as_int(key[1:-1]) or 7
        return (today - timedelta(days=n)).isoformat(), today.isoformat(), key
    return first.isoformat(), today.isoformat(), "本月"


def _like(model, fields: tuple[str, ...], keyword: str):
    like = f"%{keyword}%"
    return or_(*[getattr(model, f).like(like) for f in fields])


# ------------------------------------------------------------------ 名称 → id
#
# 用户说的是「张斌」「xts」「商务宴请」，系统要的是 id。早先的做法是把 id 清单塞进
# system prompt 让模型自己找，一旦清单被截断（客户多了就会）就变成「系统里找不到这个
# 客户」——其实客户就在库里。现在改成：模型可以照抄用户的原话，由这里解析。


def _options(db: Session, model, fields: tuple[str, ...] = ("name",), limit: int = 12) -> str:
    rows = db.query(model).order_by(model.id.asc()).limit(limit).all()
    if not rows:
        return "（还没有任何记录）"
    return "、".join(f"{getattr(r, fields[0])}(id={r.id})" for r in rows)


def _pick(db: Session, model, value, label: str, fields: tuple[str, ...] = ("name",)):
    """按 id / 精确名 / 模糊名 找一条记录。找不到或撞多条时抛 ToolError（附候选）。"""
    if value in (None, "", [], {}):
        return None
    oid = _as_int(value)
    if oid is not None:
        obj = db.get(model, oid)
        if obj is not None:
            return obj
    name = str(value).strip()
    if not name:
        return None
    exact = db.query(model).filter(or_(*[getattr(model, f) == name for f in fields])).first()
    if exact is not None:
        return exact
    upper = name.upper()
    for row in db.query(model).filter(or_(*[getattr(model, f).like(f"%{name}%") for f in fields])).limit(6):
        if str(getattr(row, fields[0])).upper() == upper:
            return row
    hit = db.query(model).filter(or_(*[getattr(model, f).like(f"%{name}%") for f in fields])).limit(6).all()
    if len(hit) == 1:
        return hit[0]
    if not hit:
        raise ToolError(f"{label}「{name}」不存在。现有：{_options(db, model, fields)}")
    names = "、".join(f"{getattr(r, fields[0])}(id={r.id})" for r in hit)
    raise ToolError(f"{label}「{name}」匹配到多条：{names}。请指明是哪一个（或直接给 id）")


def pick_employee(db: Session, value):
    return _pick(db, m.Employee, value, "员工", ("name", "employee_no"))


def pick_customer(db: Session, value):
    return _pick(db, m.Customer, value, "客户", ("name", "code"))


def pick_project(db: Session, value):
    return _pick(db, m.Project, value, "项目", ("name", "code"))


def pick_department(db: Session, value):
    return _pick(db, m.Department, value, "部门", ("name", "code"))


def pick_category(db: Session, value):
    return _pick(db, m.ExpenseCategory, value, "费用类型", ("name", "code"))


def pick_user(db: Session, value):
    return _pick(db, m.AppUser, value, "账号", ("username", "name"))


def pick_reimbursement(db: Session, value):
    """报销单：支持 id、单号 BX...、标题模糊。"""
    if value in (None, "", []):
        return None
    oid = _as_int(value)
    if oid is not None:
        obj = db.get(m.Reimbursement, oid)
        if obj is not None:
            return obj
    text = str(value).strip()
    row = (
        db.query(m.Reimbursement)
        .filter(or_(m.Reimbursement.code == text, m.Reimbursement.title == text))
        .first()
    )
    if row is not None:
        return row
    hit = (
        db.query(m.Reimbursement)
        .filter(or_(m.Reimbursement.code.like(f"%{text}%"), m.Reimbursement.title.like(f"%{text}%")))
        .order_by(m.Reimbursement.id.desc())
        .limit(6)
        .all()
    )
    if len(hit) == 1:
        return hit[0]
    if not hit:
        raise ToolError(f"没有找到报销单「{text}」。可以让用户到「报销单管理」页确认单号")
    listing = "、".join(f"{r.code}（{r.title}，{r.status}）" for r in hit)
    raise ToolError(f"「{text}」匹配到多张单：{listing}。请指明单号")


def pick_invoice(db: Session, value):
    if value in (None, "", []):
        return None
    oid = _as_int(value)
    if oid is not None:
        obj = db.get(m.Invoice, oid)
        if obj is not None:
            return obj
    text = "".join(ch for ch in str(value) if ch.isalnum())
    if not text:
        return None
    hit = db.query(m.Invoice).filter(m.Invoice.invoice_no.like(f"%{text}%")).limit(6).all()
    if len(hit) == 1:
        return hit[0]
    if not hit:
        raise ToolError(f"没有找到发票「{value}」")
    listing = "、".join(f"{v.invoice_no}(id={v.id})" for v in hit)
    raise ToolError(f"发票号「{value}」匹配到多张：{listing}")


def load_reimbursement(db: Session, oid: int) -> m.Reimbursement:
    obj = (
        db.query(m.Reimbursement)
        .populate_existing()
        .options(
            selectinload(m.Reimbursement.items).selectinload(m.ReimbursementItem.invoices),
            selectinload(m.Reimbursement.items).selectinload(m.ReimbursementItem.category),
            selectinload(m.Reimbursement.invoices),
            selectinload(m.Reimbursement.logs),
            selectinload(m.Reimbursement.applicant),
            selectinload(m.Reimbursement.department),
            selectinload(m.Reimbursement.customer),
            selectinload(m.Reimbursement.project),
        )
        .filter(m.Reimbursement.id == oid)
        .first()
    )
    if not obj:
        raise ToolError("报销单不存在")
    return obj


def _scope_reimbursement(ctx: Ctx, value) -> m.Reimbursement:
    """解析 + 校验可见性，所有涉及报销单的动作都从这里过。"""
    obj = pick_reimbursement(ctx.db, value)
    if obj is None:
        raise ToolError("缺少「报销单」，请给单号或 id")
    sec.ensure_can_view(ctx.user, obj)
    return obj


def _scope_invoice(ctx: Ctx, value) -> m.Invoice:
    obj = pick_invoice(ctx.db, value)
    if obj is None:
        raise ToolError("缺少「发票」，请给发票 id 或号码")
    conds = ctx.invoice_scope
    if conds:
        ok = (
            ctx.db.query(m.Invoice)
            .filter(m.Invoice.id == obj.id, *conds)
            .count()
        )
        if not ok:
            raise ToolError("这张发票不在你的可见范围内")
    return obj


# ================================================================== 只读工具


READ_TOOLS: dict[str, dict] = {}


def read_tool(name: str, label: str, desc: str, params: dict, roles=ALL_ROLES):
    def deco(fn: Callable[[Ctx, dict], Any]):
        READ_TOOLS[name] = {
            "name": name, "label": label, "desc": desc, "params": params,
            "roles": tuple(roles), "fn": fn,
        }
        return fn

    return deco


def _sum(rows, key="amount") -> float:
    return round(sum(float(r.get(key) or 0) for r in rows), 2)


@read_tool(
    "list_master",
    "查询主数据",
    "查部门/员工/费用类型/客户/项目/预算的清单与 id。不知道某个名字对应的 id 时用它。",
    {
        "entity": "必填：departments|employees|categories|customers|projects|budgets",
        "keyword": "选填，按名称/工号/编码模糊匹配",
        "active_only": "选填 bool，只列启用的（员工/费用类型）",
        "limit": "选填 int，默认 50",
    },
)
def t_list_master(ctx: Ctx, p: dict) -> dict:
    entity = str(p.get("entity") or "customers").strip().lower()
    keyword = str(p.get("keyword") or "").strip()
    limit = _as_int(p.get("limit")) or 50
    db = ctx.db

    if entity in ("departments", "department", "部门"):
        q = db.query(m.Department)
        if keyword:
            q = q.filter(_like(m.Department, ("name", "code", "manager"), keyword))
        rows = q.order_by(m.Department.id).limit(limit).all()
        return {"entity": "部门", "count": len(rows), "items": [ser.department_out(r) for r in rows]}

    if entity in ("employees", "employee", "员工"):
        q = db.query(m.Employee).options(selectinload(m.Employee.department))
        if keyword:
            q = q.filter(_like(m.Employee, ("name", "employee_no", "position"), keyword))
        if p.get("active_only"):
            q = q.filter(m.Employee.active.is_(True))
        rows = q.order_by(m.Employee.id).limit(limit).all()
        return {"entity": "员工", "count": len(rows), "items": [ser.employee_out(r) for r in rows]}

    if entity in ("categories", "category", "费用类型"):
        q = db.query(m.ExpenseCategory)
        if keyword:
            q = q.filter(_like(m.ExpenseCategory, ("name", "code", "group_name"), keyword))
        if p.get("active_only"):
            q = q.filter(m.ExpenseCategory.active.is_(True))
        rows = q.order_by(m.ExpenseCategory.id).limit(limit).all()
        return {"entity": "费用类型", "count": len(rows), "items": [ser.category_out(r) for r in rows]}

    if entity in ("customers", "customer", "客户"):
        q = db.query(m.Customer)
        if keyword:
            q = q.filter(_like(m.Customer, ("name", "code", "contact"), keyword))
        rows = q.order_by(m.Customer.id).limit(limit).all()
        return {"entity": "客户", "count": len(rows), "items": [ser.customer_out(r) for r in rows]}

    if entity in ("projects", "project", "项目"):
        q = db.query(m.Project).options(selectinload(m.Project.customer))
        if keyword:
            q = q.filter(_like(m.Project, ("name", "code", "manager"), keyword))
        rows = q.order_by(m.Project.id).limit(limit).all()
        return {"entity": "项目", "count": len(rows), "items": [ser.project_out(r) for r in rows]}

    if entity in ("budgets", "budget", "预算"):
        if not ctx.can_role(STAFF):
            raise ToolError("预算数据仅财务与管理员可见")
        rows = (
            db.query(m.Budget)
            .filter(m.Budget.year == (_as_int(p.get("year")) or date.today().year))
            .order_by(m.Budget.month.asc().nullsfirst())
            .limit(limit)
            .all()
        )
        from .routers.master import budget_used

        return {
            "entity": "预算",
            "count": len(rows),
            "items": [ser.budget_out(b, budget_used(db, b)) for b in rows],
        }

    raise ToolError(
        "未知的主数据类型，可选：departments / employees / categories / customers / projects / budgets"
    )


@read_tool(
    "list_reimbursements",
    "查询报销单",
    "按状态、申请人、部门、客户、项目、金额、日期筛报销单。返回 id、单号、状态、金额。",
    {
        "status": "选填：草稿|待审批|已通过|已驳回|已付款，多个用逗号",
        "keyword": "选填，标题/事由/单号模糊",
        "applicant": "选填，申请人姓名或 id",
        "department": "选填，部门名或 id",
        "customer": "选填，客户名或 id",
        "project": "选填，项目名或 id",
        "amount_min": "选填 number",
        "amount_max": "选填 number",
        "date_from": "选填 YYYY-MM-DD（按费用发生日期）",
        "date_to": "选填 YYYY-MM-DD",
        "only_mine": "选填 bool，只看自己提交的",
        "limit": "选填 int，默认 20，最大 100",
    },
)
def t_list_reimbursements(ctx: Ctx, p: dict) -> dict:
    db = ctx.db
    conds = list(ctx.scope)
    if p.get("only_mine") and ctx.user.employee_id:
        conds.append(m.Reimbursement.applicant_id == ctx.user.employee_id)
    if p.get("keyword"):
        conds.append(_like(m.Reimbursement, ("title", "purpose", "code"), str(p["keyword"]).strip()))
    if p.get("status"):
        wanted = [s.strip() for s in str(p["status"]).split(",") if s.strip()]
        conds.append(m.Reimbursement.status.in_(wanted))
    emp = pick_employee(db, p.get("applicant"))
    if emp is not None:
        conds.append(m.Reimbursement.applicant_id == emp.id)
    dept = pick_department(db, p.get("department"))
    if dept is not None:
        conds.append(m.Reimbursement.department_id == dept.id)
    cust = pick_customer(db, p.get("customer"))
    if cust is not None:
        conds.append(m.Reimbursement.customer_id == cust.id)
    proj = pick_project(db, p.get("project"))
    if proj is not None:
        conds.append(m.Reimbursement.project_id == proj.id)
    if _as_float(p.get("amount_min")) is not None:
        conds.append(m.Reimbursement.total_amount >= _as_float(p["amount_min"]))
    if _as_float(p.get("amount_max")) is not None:
        conds.append(m.Reimbursement.total_amount <= _as_float(p["amount_max"]))
    if _as_date(p.get("date_from")):
        conds.append(m.Reimbursement.occur_start >= _as_date(p["date_from"]))
    if _as_date(p.get("date_to")):
        conds.append(m.Reimbursement.occur_end <= _as_date(p["date_to"]))

    q = (
        db.query(m.Reimbursement)
        .options(selectinload(m.Reimbursement.applicant), selectinload(m.Reimbursement.department))
        .filter(*conds)
    )
    total = q.count()
    sum_amount = ser.money(
        q.with_entities(func.coalesce(func.sum(m.Reimbursement.total_amount), 0)).scalar()
    )
    limit = _limit_of(p.get("limit"))
    rows = q.order_by(m.Reimbursement.id.desc()).limit(limit).all()
    return {
        "total": total,
        "amount_sum": sum_amount,
        "returned": len(rows),
        "items": [ser.reimbursement_out(r) for r in rows],
        "note": "只返回最近的部分结果，需要更精确请缩小筛选条件" if total > len(rows) else "",
    }


@read_tool(
    "get_reimbursement",
    "查看报销单详情",
    "拿到某张单的完整信息：明细、关联发票、审批流、金额构成。",
    {"reimbursement": "必填：单号 BX... 或 id"},
)
def t_get_reimbursement(ctx: Ctx, p: dict) -> dict:
    obj = _scope_reimbursement(ctx, p.get("reimbursement"))
    obj = load_reimbursement(ctx.db, obj.id)
    return ser.reimbursement_detail(obj)


@read_tool(
    "list_invoices",
    "查询发票",
    "按是否关联报销单、查验状态、费用类型、销售方、金额、日期筛发票。",
    {
        "unlinked_only": "选填 bool，只看没关联报销单的散票",
        "check_status": "选填：未查验|已查验|异常",
        "category": "选填，费用类型名或 id",
        "keyword": "选填，发票号码/销售方/购买方模糊",
        "date_from": "选填 YYYY-MM-DD（开票日期）",
        "date_to": "选填 YYYY-MM-DD",
        "amount_min": "选填 number",
        "amount_max": "选填 number",
        "limit": "选填 int，默认 20，最大 100",
    },
)
def t_list_invoices(ctx: Ctx, p: dict) -> dict:
    db = ctx.db
    conds = list(ctx.invoice_scope)
    if p.get("unlinked_only"):
        conds.append(m.Invoice.reimbursement_id.is_(None))
    if p.get("check_status"):
        conds.append(m.Invoice.check_status == str(p["check_status"]).strip())
    if p.get("keyword"):
        conds.append(
            _like(m.Invoice, ("invoice_no", "seller_name", "buyer_name", "invoice_code"),
                  str(p["keyword"]).strip())
        )
    cat = pick_category(db, p.get("category"))
    if cat is not None:
        conds.append(m.Invoice.category_id == cat.id)
    if _as_date(p.get("date_from")):
        conds.append(m.Invoice.invoice_date >= _as_date(p["date_from"]))
    if _as_date(p.get("date_to")):
        conds.append(m.Invoice.invoice_date <= _as_date(p["date_to"]))
    if _as_float(p.get("amount_min")) is not None:
        conds.append(m.Invoice.amount >= _as_float(p["amount_min"]))
    if _as_float(p.get("amount_max")) is not None:
        conds.append(m.Invoice.amount <= _as_float(p["amount_max"]))

    q = (
        db.query(m.Invoice)
        .options(selectinload(m.Invoice.category), selectinload(m.Invoice.reimbursement))
        .filter(*conds)
    )
    total = q.count()
    sum_amount = ser.money(q.with_entities(func.coalesce(func.sum(m.Invoice.amount), 0)).scalar())
    limit = _limit_of(p.get("limit"))
    rows = q.order_by(m.Invoice.id.desc()).limit(limit).all()
    return {
        "total": total,
        "amount_sum": sum_amount,
        "returned": len(rows),
        "items": [ser.invoice_out(v) for v in rows],
    }


@read_tool(
    "get_invoice",
    "查看发票详情",
    "按 id 或发票号码拿单张发票的完整信息。",
    {"invoice": "必填：发票 id 或发票号码"},
)
def t_get_invoice(ctx: Ctx, p: dict) -> dict:
    obj = _scope_invoice(ctx, p.get("invoice"))
    db = ctx.db
    row = (
        db.query(m.Invoice)
        .populate_existing()
        .options(selectinload(m.Invoice.category), selectinload(m.Invoice.reimbursement),
                 selectinload(m.Invoice.attachments))
        .filter(m.Invoice.id == obj.id)
        .first()
    )
    return ser.invoice_out(row)


@read_tool(
    "list_alerts",
    "异常与预警",
    "算一遍系统里的异常点：超限额、缺发票、问题发票、大额单、超期未审批。",
    {
        "kind": "选填：all|over_limit|missing_invoice|bad_invoice|large_amount|overdue，默认 all",
        "limit": "选填 int，默认 20",
    },
    roles=(m.ROLE_APPROVER, m.ROLE_FINANCE, m.ROLE_ADMIN),
)
def t_list_alerts(ctx: Ctx, p: dict) -> dict:
    from .routers import stats

    data = stats.alerts(
        large_amount=_as_float(p.get("large_amount")) or 20000.0,
        overdue_days=_as_int(p.get("overdue_days")) or 7,
        _analyst=ctx.user,
        scope=ctx.scope,
        db=ctx.db,
    )
    kind = str(p.get("kind") or "all").strip()
    limit = max(1, min(_as_int(p.get("limit")) or 20, 50))
    if kind in ("all", "", "全部"):
        out = {k: (v[:limit] if isinstance(v, list) else v) for k, v in data.items()}
        return out
    mapping = {
        "over_limit": "over_limit", "超限额": "over_limit",
        "missing_invoice": "missing_invoice", "缺发票": "missing_invoice",
        "bad_invoice": "bad_invoices", "问题发票": "bad_invoices", "bad_invoices": "bad_invoices",
        "large_amount": "large_amount", "大额": "large_amount",
        "overdue": "overdue", "超期": "overdue",
    }
    key = mapping.get(kind)
    if not key:
        raise ToolError("kind 只能是 all/over_limit/missing_invoice/bad_invoice/large_amount/overdue")
    return {"kind": key, "summary": data["summary"], "items": data[key][:limit]}


@read_tool(
    "get_stats",
    "统计数据",
    "看板口径的统计：概览（含本月环比上月同期）、趋势、按费用类型/部门/员工/客户/项目分布、预算执行。",
    {
        "kind": "必填：overview|trend|by_category|by_group|by_department|by_employee|by_customer|by_project|budget_execution",
        "period": "选填：本月|上月|本年|近3月|近7天|全部（默认本月；trend 类按月份数）",
        "months": "选填 int，trend/月度分布用，默认 6",
        "year": "选填 int，预算执行用",
        "department": "选填，部门名或 id",
        "status_filter": "选填：已通过|已付款 等，多个用逗号",
        "limit": "选填 int",
    },
    roles=ALL_ROLES,
)
def t_get_stats(ctx: Ctx, p: dict) -> dict:
    from .routers import stats

    kind = str(p.get("kind") or "overview").strip()
    db = ctx.db
    date_from, date_to, label = _period(p)
    dept = pick_department(db, p.get("department"))
    dept_id = dept.id if dept else None

    if kind == "overview":
        data = stats.overview(date_from=date_from, date_to=date_to, department_id=dept_id,
                              scope=ctx.scope, db=db)
        data["period"] = label
        return data
    if kind == "trend":
        return {"period": label, "items": stats.trend(
            months=max(1, min(_as_int(p.get("months")) or 6, 36)),
            date_from=date_from, date_to=date_to, department_id=dept_id,
            scope=ctx.scope, db=db)}
    if kind in ("by_category", "by_group", "by_department", "by_employee", "by_customer", "by_project"):
        fn = {
            "by_category": stats.by_category,
            "by_group": stats.by_group,
            "by_department": stats.by_department,
            "by_employee": stats.by_employee,
            "by_customer": stats.by_customer,
            "by_project": stats.by_project,
        }[kind]
        kwargs = {"date_from": date_from, "date_to": date_to, "scope": ctx.scope, "db": db}
        if kind in ("by_category", "by_group"):
            kwargs["department_id"] = dept_id
            kwargs["status_filter"] = p.get("status_filter") or None
        if kind in ("by_employee", "by_customer", "by_project"):
            kwargs["limit"] = max(1, min(_as_int(p.get("limit")) or 15, 100))
        return {"period": label, "kind": kind, "items": fn(**kwargs)}
    if kind == "budget_execution":
        if not ctx.can_role(STAFF):
            raise ToolError("预算执行只有财务与管理员能看")
        return {"items": stats.budget_execution(
            year=_as_int(p.get("year")) or date.today().year, _analyst=ctx.user, db=db)}
    raise ToolError(
        "kind 只能是 overview/trend/by_category/by_group/by_department/by_employee/"
        "by_customer/by_project/budget_execution"
    )


@read_tool(
    "list_expense_items",
    "费用明细台账",
    "跨报销单的费用行视角：谁、什么时候、花了什么钱。",
    {
        "keyword": "选填，摘要/单号/标题模糊",
        "category": "选填，费用类型名或 id",
        "department": "选填，部门名或 id",
        "status": "选填，报销单状态，多个用逗号",
        "period": "选填：本月|上月|本年|近3月|全部",
        "limit": "选填 int，默认 20",
    },
)
def t_list_items(ctx: Ctx, p: dict) -> dict:
    from .routers.master import list_items

    cat = pick_category(ctx.db, p.get("category"))
    dept = pick_department(ctx.db, p.get("department"))
    date_from, date_to, label = _period(p)
    limit = _limit_of(p.get("limit"))
    data = list_items(
        q=p.get("keyword") or None,
        category_id=cat.id if cat else None,
        department_id=dept.id if dept else None,
        customer_id=None, project_id=None,
        status=str(p["status"]) if p.get("status") else None,
        date_from=date_from, date_to=date_to, sort="date_desc",
        page=1, page_size=limit, user=ctx.user, db=ctx.db,
    )
    data["period"] = label
    return data


@read_tool(
    "list_inbox",
    "发票收件箱",
    "看收票邮箱的收取记录与同步状态，回答「票收进来没有 / 为什么没自动入账」。",
    {"keyword": "选填，主题/发件人", "status": "选填：成功|部分成功|失败|跳过", "limit": "选填 int，默认 10"},
    roles=STAFF,
)
def t_list_inbox(ctx: Ctx, p: dict) -> dict:
    from .routers.inbox import list_messages

    limit = max(1, min(_as_int(p.get("limit")) or 10, 50))
    data = list_messages(
        q=p.get("keyword") or None, status=p.get("status") or None, account_id=None,
        date_from=None, date_to=None, page=1, page_size=limit, db=ctx.db,
    )
    accounts = [
        {
            "id": a.id, "name": a.name, "username": a.username, "active": a.active,
            "last_sync_at": ser.dt_s(a.last_sync_at), "last_sync_status": a.last_sync_status,
            "imported_total": a.imported_total,
        }
        for a in ctx.db.query(m.MailAccount).order_by(m.MailAccount.id).all()
    ]
    return {"summary": data["summary"], "accounts": accounts, "messages": data["items"]}


@read_tool(
    "list_users",
    "账号列表",
    "看系统里有哪些登录账号、什么角色、是否绑定员工、是否停用。",
    {"keyword": "选填，用户名/姓名", "role": "选填，角色名", "limit": "选填 int，默认 30"},
    roles=ADMIN_ONLY,
)
def t_list_users(ctx: Ctx, p: dict) -> dict:
    q = ctx.db.query(m.AppUser)
    if p.get("keyword"):
        q = q.filter(_like(m.AppUser, ("username", "name"), str(p["keyword"]).strip()))
    if p.get("role"):
        q = q.filter(m.AppUser.role == str(p["role"]).strip())
    rows = q.order_by(m.AppUser.id).limit(max(1, min(_as_int(p.get("limit")) or 30, 100))).all()
    return {
        "count": len(rows),
        "roles": m.ROLES,
        "items": [
            {
                "id": u.id, "username": u.username, "name": u.name, "role": u.role,
                "employee_id": u.employee_id, "approval_level": u.approval_level,
                "active": u.active, "must_change_password": u.must_change_password,
                "last_login_at": ser.dt_s(u.last_login_at),
            }
            for u in rows
        ],
    }


@read_tool(
    "get_settings",
    "系统参数",
    "读系统参数：二级审批阈值、大额预警线、超期天数等，以及当前审批规则摘要。",
    {},
    roles=(m.ROLE_FINANCE, m.ROLE_ADMIN),
)
def t_get_settings(ctx: Ctx, p: dict) -> dict:
    from . import approvals as ap

    ap.ensure_default_settings(ctx.db)
    rows = ctx.db.query(m.Setting).order_by(m.Setting.key).all()
    return {
        "approval": ap.approval_summary(ctx.db),
        "items": [{"key": r.key, "value": r.value, "remark": r.remark} for r in rows],
    }


@read_tool(
    "list_audit_logs",
    "操作审计",
    "查操作日志：谁在什么时候做了什么（写操作、登录、参数变更）。",
    {
        "keyword": "选填，操作内容/实体 id",
        "username": "选填，账号",
        "action": "选填，动作关键词，如「审批」「删除」",
        "period": "选填：今天|近7天|近30天|本月|全部，默认近7天",
        "limit": "选填 int，默认 20",
    },
    roles=ADMIN_ONLY,
)
def t_list_audit(ctx: Ctx, p: dict) -> dict:
    db = ctx.db
    q = db.query(m.AuditLog)
    if p.get("keyword"):
        q = q.filter(_like(m.AuditLog, ("action", "entity_id", "detail", "path"), str(p["keyword"]).strip()))
    if p.get("username"):
        q = q.filter(m.AuditLog.username == str(p["username"]).strip())
    if p.get("action"):
        q = q.filter(m.AuditLog.action.like(f"%{str(p['action']).strip()}%"))
    date_from, _, label = _period({"period": p.get("period") or "近7天"})
    if _as_date(date_from):
        q = q.filter(m.AuditLog.created_at >= datetime.combine(_as_date(date_from), datetime.min.time()))
    limit = max(1, min(_as_int(p.get("limit")) or 20, 100))
    total = q.count()
    rows = q.order_by(m.AuditLog.id.desc()).limit(limit).all()
    return {
        "period": label,
        "total": total,
        "items": [
            {
                "id": r.id, "username": r.username, "role": r.role, "action": r.action,
                "entity": r.entity, "entity_id": r.entity_id, "method": r.method,
                "path": r.path, "status_code": r.status_code, "detail": r.detail,
                "created_at": ser.dt_s(r.created_at),
            }
            for r in rows
        ],
    }


@read_tool(
    "search",
    "全局搜索",
    "拿一个关键词到处找：报销单、发票、员工、客户、项目都对一遍。名字记不准时先用它。",
    {"keyword": "必填，关键词", "limit": "选填 int，每类最多几条，默认 5"},
)
def t_search(ctx: Ctx, p: dict) -> dict:
    db = ctx.db
    kw = str(p.get("keyword") or "").strip()
    if not kw:
        raise ToolError("请给出要搜索的关键词")
    limit = max(1, min(_as_int(p.get("limit")) or 5, 20))
    like = f"%{kw}%"

    rs = (
        db.query(m.Reimbursement)
        .filter(*ctx.scope)
        .filter(or_(m.Reimbursement.title.like(like), m.Reimbursement.purpose.like(like),
                    m.Reimbursement.code.like(like)))
        .order_by(m.Reimbursement.id.desc())
        .limit(limit)
        .all()
    )
    vs = (
        db.query(m.Invoice)
        .filter(*ctx.invoice_scope)
        .filter(or_(m.Invoice.invoice_no.like(like), m.Invoice.seller_name.like(like),
                    m.Invoice.buyer_name.like(like)))
        .order_by(m.Invoice.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "reimbursements": [ser.reimbursement_out(r) for r in rs],
        "invoices": [ser.invoice_out(v) for v in vs],
        "employees": [
            ser.employee_out(e)
            for e in db.query(m.Employee)
            .options(selectinload(m.Employee.department))
            .filter(_like(m.Employee, ("name", "employee_no"), kw))
            .limit(limit)
        ],
        "customers": [ser.customer_out(c) for c in db.query(m.Customer).filter(
            _like(m.Customer, ("name", "code", "contact"), kw)).limit(limit)],
        "projects": [ser.project_out(x) for x in db.query(m.Project).filter(
            _like(m.Project, ("name", "code", "manager"), kw)).limit(limit)],
    }


@read_tool(
    "analyze_reimbursement",
    "AI 分析报销单",
    "对一张报销单做合规与风险分析（用的是另一个大模型调用，只读、不改数据）。",
    {"reimbursement": "必填：单号或 id"},
)
def t_analyze(ctx: Ctx, p: dict) -> dict:
    from . import ai as ai_mod
    from . import ai_service, ai_tasks
    from .routers.ai import _alerts_for

    obj = load_reimbursement(ctx.db, _scope_reimbursement(ctx, p.get("reimbursement")).id)
    try:
        cfg = ai_service.ensure_provider(ctx.db, "analyze")
    except ai_mod.AiError as exc:
        raise ToolError(str(exc)) from None
    payload = ai_service.reimbursement_payload(obj)
    payload["系统已算出的异常提示"] = _alerts_for(ctx.db, obj)
    try:
        got = ai_tasks.analyze_reimbursement(cfg, payload)
    except ai_mod.AiError as exc:
        raise ToolError(str(exc)) from None
    ai_mod.record_usage(ctx.db, user=ctx.user, kind=m.AI_ANALYZE, cfg=cfg, ok=True,
                        usage=got["usage"], detail=obj.code)
    return {
        "code": obj.code, "summary": got["summary"], "level": got["level"],
        "findings": got["findings"], "suggestions": got["suggestions"],
    }


@read_tool(
    "approval_advice",
    "AI 审批建议",
    "对待审批的单据出一份参考意见（不代替审批、不会自动审批）。",
    {"reimbursement": "必填：单号或 id"},
    roles=APPROVERS,
)
def t_advice(ctx: Ctx, p: dict) -> dict:
    from . import ai as ai_mod
    from . import ai_service, ai_tasks
    from .routers.ai import _alerts_for

    obj = load_reimbursement(ctx.db, _scope_reimbursement(ctx, p.get("reimbursement")).id)
    try:
        cfg = ai_service.ensure_provider(ctx.db, "analyze")
    except ai_mod.AiError as exc:
        raise ToolError(str(exc)) from None
    payload = ai_service.reimbursement_payload(obj)
    payload["系统已算出的异常提示"] = _alerts_for(ctx.db, obj)
    payload["审批人视角"] = {
        "审批人": ctx.user.name, "角色": ctx.user.role,
        "当前待批级别": obj.approved_level + 1, "需要级别": obj.required_level,
    }
    try:
        got = ai_tasks.approval_advice(cfg, payload)
    except ai_mod.AiError as exc:
        raise ToolError(str(exc)) from None
    ai_mod.record_usage(ctx.db, user=ctx.user, kind=m.AI_APPROVAL, cfg=cfg, ok=True,
                        usage=got["usage"], detail=obj.code)
    return {
        "code": obj.code, "recommend": got["recommend"], "confidence": got["confidence"],
        "reasons": got["reasons"], "risks": got["risks"], "questions": got["questions"],
        "disclaimer": "以上为模型给出的参考意见，最终决定权在审批人。",
    }


def tools_for(user) -> list[dict]:
    return [t for t in READ_TOOLS.values() if user.role in t["roles"]]


def run_read(ctx: Ctx, name: str, params: dict) -> dict:
    tool = READ_TOOLS.get(str(name or "").strip())
    if tool is None:
        raise ToolError(f"没有名为「{name}」的查询工具")
    if ctx.user.role not in tool["roles"]:
        raise ToolError(f"当前角色「{ctx.user.role}」不能使用「{tool['label']}」")
    result = tool["fn"](ctx, params or {})
    if isinstance(result, dict):
        result.setdefault("_tool", tool["name"])
    return result


# ================================================================== 写动作
#
# 每个动作 = 一张确认卡片。`prepare` 负责在卡片出现之前把参数弄干净：
#   - 名称/id 互转、缺省值补全（申请人默认当前登录用户绑定的员工）
#   - 必填校验、状态可编辑性校验、目标记录可见性校验
#   - 返回 (规范化后的 params, 提示语列表)
# prepare 抛 ToolError 时，卡片不会出现，而是把原因回灌给模型，让它去问用户。


WRITE_ACTIONS: dict[str, dict] = {}


def write_action(type_: str, label: str, group: str, params: dict, roles=ALL_ROLES,
                 danger: bool = False, prepare: Callable[[Ctx, dict], Any] | None = None,
                 note: str = ""):
    def deco(fn: Callable[[Ctx, dict], tuple[dict, list[str]]]):
        WRITE_ACTIONS[type_] = {
            "type": type_, "label": label, "group": group, "params": params,
            "roles": tuple(roles), "danger": bool(danger), "prepare": fn, "note": note,
        }
        return fn

    return deco


def _need(params: dict, key: str, label: str):
    value = params.get(key)
    if value in (None, "", [], {}):
        raise ToolError(f"还差「{label}」，请告诉我再继续")
    return value


# ---------------------------------------------------------------- 报销单


@write_action(
    "create_reimbursement", "新建报销单", "报销单", roles=ALL_ROLES,
    params={
        "title": "str 必填，简洁体现事由",
        "purpose": "str 选填，事由说明",
        "applicant": "str 选填，申请人姓名或 id（**代他人建单时必须给**；不给自己建就用当前登录账号绑定的员工）",
        "department": "str 选填，部门名或 id（不给则按申请人带出）",
        "customer": "str 选填，客户名或 id",
        "project": "str 选填，项目名或 id",
        "occur_start": "YYYY-MM-DD 选填，费用起始日",
        "occur_end": "YYYY-MM-DD 选填",
        "remark": "str 选填",
        "items": "list 必填，明细行 [{category: 费用类型名或 id, occur_date: YYYY-MM-DD, "
                 "amount: number, tax_amount: number, description: str}]",
        "invoice_ids": "int[] 选填，要顺带关联的发票 id",
    },
    note="建出来是草稿，发票可以稍后再关联",
)
def p_create_reimbursement(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    db = ctx.db
    warns: list[str] = []
    title = str(p.get("title") or p.get("purpose") or "").strip()
    if not title:
        raise ToolError("这张单准备报什么事？给我一个标题（比如「XTS 招待晚餐」）")

    applicant = None
    if p.get("applicant") not in (None, "", []):
        applicant = pick_employee(db, p["applicant"])
    if applicant is None:
        if ctx.user.employee_id:
            applicant = db.get(m.Employee, ctx.user.employee_id)
            if applicant is not None:
                warns.append(f"申请人默认用了当前登录账号绑定的员工「{applicant.name}」")
        if applicant is None:
            raise ToolError(
                "当前账号没有绑定员工，无法确定申请人。请指明申请人姓名，"
                "或让管理员在「用户管理」里给这个账号绑定员工"
            )
    applicant_id = applicant.id

    dept = pick_department(db, p.get("department"))
    if dept is None and applicant.department_id:
        dept = db.get(m.Department, applicant.department_id)
        if dept is not None:
            warns.append(f"部门按申请人带出为「{dept.name}」")
    cust = pick_customer(db, p.get("customer"))
    proj = pick_project(db, p.get("project"))

    raw_items = p.get("items")
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list) or not raw_items:
        raise ToolError("还差费用明细：至少要有一行（费用类型、金额、发生日期）")
    items: list[dict] = []
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        cat = pick_category(db, row.get("category") or row.get("category_id"))
        amount = _as_float(row.get("amount"))
        if amount is None:
            raise ToolError(f"明细行「{row.get('description') or ''}」没有金额")
        items.append({
            "category_id": cat.id if cat else None,
            "occur_date": (_as_date(row.get("occur_date"), date.today()) or date.today()).isoformat(),
            "amount": amount,
            "tax_amount": _as_float(row.get("tax_amount")) or 0.0,
            "description": str(row.get("description") or title)[:200],
        })
    if not items:
        raise ToolError("费用明细解析后是空的，请再确认一下费用类型与金额")
    if not any(it["category_id"] for it in items):
        warns.append("没有任何一行落到已知的费用类型上，到「费用类型」页补充后再选")

    total = round(sum(it["amount"] for it in items), 2)
    dates = [it["occur_date"] for it in items]
    params = {
        "title": title[:120],
        "purpose": str(p.get("purpose") or "").strip()[:500] or None,
        "applicant_id": applicant_id,
        "department_id": dept.id if dept else None,
        "customer_id": cust.id if cust else None,
        "project_id": proj.id if proj else None,
        "occur_start": p.get("occur_start") and _as_date(p["occur_start"]).isoformat() or min(dates),
        "occur_end": p.get("occur_end") and _as_date(p["occur_end"]).isoformat() or max(dates),
        "remark": str(p.get("remark") or "").strip()[:500] or None,
        "items": items,
        "invoice_ids": [n for n in (_as_int(i) for i in (p.get("invoice_ids") or [])) if n],
        "_amount": total,
    }
    if params["invoice_ids"]:
        warns.append(f"会顺带把这 {len(params['invoice_ids'])} 张发票关联上去")
    else:
        warns.append("未关联发票，发票到了再关联也不影响")
    return params, warns


@write_action(
    "update_reimbursement", "修改报销单", "报销单", roles=ALL_ROLES,
    params={
        "reimbursement": "必填，单号或 id",
        "title": "str 选填", "purpose": "str 选填", "applicant": "str 选填",
        "department": "str 选填", "customer": "str 选填", "project": "str 选填",
        "occur_start": "YYYY-MM-DD 选填", "occur_end": "YYYY-MM-DD 选填",
        "remark": "str 选填",
        "items": "list 选填；给了就按新明细整体替换（原明细行的发票关联会保留在被保留的行上）",
    },
)
def p_update_reimbursement(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    db = ctx.db
    obj = _scope_reimbursement(ctx, p.get("reimbursement"))
    if obj.status not in (m.ST_DRAFT, m.ST_REJECTED):
        raise ToolError(f"当前状态「{obj.status}」不可编辑，需要先撤回或驳回")
    warns: list[str] = []
    # expire_on_commit=False 的老问题：关系字段可能残留旧值，按 id 回查一次
    x = obj.applicant or (db.get(m.Employee, obj.applicant_id) if obj.applicant_id else None)

    applicant = pick_employee(db, p.get("applicant")) if p.get("applicant") else x
    if applicant is None:
        raise ToolError("这张单没有申请人，请指明申请人姓名")
    dept = pick_department(db, p.get("department")) if p.get("department") else obj.department
    if dept is None and applicant.department_id:
        dept = db.get(m.Department, applicant.department_id)
    cust = pick_customer(db, p.get("customer")) if p.get("customer") else obj.customer
    proj = pick_project(db, p.get("project")) if p.get("project") else obj.project

    existing = [
        {
            "id": it.id,
            "category_id": it.category_id,
            "occur_date": it.occur_date.isoformat() if it.occur_date else None,
            "amount": ser.money(it.amount),
            "tax_amount": ser.money(it.tax_amount),
            "description": it.description,
            "remark": it.remark,
        }
        for it in obj.items
    ]
    items = existing
    raw = p.get("items")
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list) and raw:
        if len(raw) == len(existing):
            items = []
            for idx, row in enumerate(raw):
                base = dict(existing[idx])
                if isinstance(row, dict):
                    cat = pick_category(db, row.get("category") or row.get("category_id"))
                    base.update({
                        "category_id": cat.id if cat else base.get("category_id"),
                        "occur_date": (_as_date(row.get("occur_date")) or _as_date(base.get("occur_date"))
                                       or date.today()).isoformat(),
                        "amount": _as_float(row.get("amount")) if _as_float(row.get("amount")) is not None
                        else base.get("amount"),
                        "tax_amount": _as_float(row.get("tax_amount")) if _as_float(row.get("tax_amount")) is not None
                        else base.get("tax_amount"),
                        "description": str(row.get("description") or base.get("description") or "")[:200],
                    })
                else:
                    base = {"id": None, "category_id": None, "occur_date": None,
                            "amount": None, "tax_amount": 0, "description": str(row)[:200]}
                items.append(base)
        else:
            warns.append("明细行数量变了，将整体替换；原有明细行上的发票关联会被解除")
            items = [
                {
                    "id": None,
                    "category_id": (lambda c: c.id if c else None)(
                        pick_category(db, r.get("category") or r.get("category_id"))),
                    "occur_date": (_as_date(r.get("occur_date"), date.today()) or date.today()).isoformat(),
                    "amount": _as_float(r.get("amount")) or 0.0,
                    "tax_amount": _as_float(r.get("tax_amount")) or 0.0,
                    "description": str(r.get("description") or obj.title)[:200],
                }
                for r in raw if isinstance(r, dict)
            ]
    params = {
        "id": obj.id,
        "title": str(p.get("title") or obj.title),
        "purpose": p.get("purpose") if p.get("purpose") is not None else obj.purpose,
        "applicant_id": applicant.id,
        "department_id": dept.id if dept else None,
        "customer_id": cust.id if cust else None,
        "project_id": proj.id if proj else None,
        "occur_start": (p.get("occur_start") and _as_date(p["occur_start"]).isoformat())
        or (obj.occur_start.isoformat() if obj.occur_start else None),
        "occur_end": (p.get("occur_end") and _as_date(p["occur_end"]).isoformat())
        or (obj.occur_end.isoformat() if obj.occur_end else None),
        "remark": p.get("remark") if p.get("remark") is not None else obj.remark,
        "items": items,
    }
    warns.append(f"改的是 {obj.code}（{obj.status}）")
    return params, warns


def _prepare_progress(p: dict, *, comment_required: bool = False, roles=None):
    def fn(ctx: Ctx, params: dict) -> tuple[dict, list[str]]:
        obj = _scope_reimbursement(ctx, params.get("reimbursement") or params.get("reimbursement_id"))
        comment = str(params.get("comment") or "").strip()
        if comment_required and not comment:
            raise ToolError("驳回必须写原因，请告诉我为什么驳回")
        return (
            {"reimbursement_id": obj.id, "code": obj.code, "status": obj.status,
             "comment": comment or None, "_amount": ser.money(obj.total_amount)},
            [f"目标单据：{obj.code}（{obj.title}，当前 {obj.status}，{ser.money(obj.total_amount):,.2f} 元）"],
        )

    return fn


for _type, _label, _roles, _req, _danger, _note in [
    ("submit_reimbursement", "提交报销单", ALL_ROLES, False, False, "提交后进入审批流，不能直接改"),
    ("withdraw_reimbursement", "撤回报销单", ALL_ROLES, False, False, "撤回到草稿状态"),
    ("approve_reimbursement", "审批通过", APPROVERS, False, False, "通过后按级别流转"),
    ("reject_reimbursement", "驳回报销单", APPROVERS, True, True, "驳回必须填原因"),
    ("pay_reimbursement", "登记付款", STAFF, False, True, "付款后计入已付款金额"),
    ("unpay_reimbursement", "撤销付款", STAFF, False, True, "退回到已通过状态"),
    ("delete_reimbursement", "删除报销单", ALL_ROLES, False, True, "只删单据，发票会解除关联保留下来"),
]:
    WRITE_ACTIONS[_type] = {
        "type": _type, "label": _label, "group": "报销单/审批",
        "params": {"reimbursement": "必填，单号或 id",
                   **({"comment": "str 必填，驳回原因"} if _req else {"comment": "str 选填"})},
        "roles": tuple(_roles), "danger": _danger, "note": _note,
        "prepare": _prepare_progress(_type, comment_required=_req),
    }


# ---------------------------------------------------------------- 发票


@write_action(
    "create_invoice", "登记发票", "发票", roles=STAFF,
    params={
        "invoice_no": "str 必填，发票号码",
        "invoice_type": "str 选填，默认增值税电子普通发票",
        "amount": "number 必填，价税合计",
        "tax_amount": "number 选填", "tax_rate": "number 选填（百分数，13 表示 13%）",
        "invoice_date": "YYYY-MM-DD 选填",
        "seller_name": "str 选填", "seller_tax_no": "str 选填",
        "buyer_name": "str 选填（自己公司）",
        "category": "str 选填，费用类型名或 id",
        "remark": "str 选填",
    },
    note="手工登记没有票面文件，后续可在发票页补传影像",
)
def p_create_invoice(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    db = ctx.db
    no = "".join(ch for ch in str(_need(p, "invoice_no", "发票号码") or "") if ch.isalnum())
    if len(no) < 6:
        raise ToolError("发票号码看起来不对（至少 6 位数字/字母）")
    if db.query(m.Invoice).filter(m.Invoice.invoice_no == no).count():
        raise ToolError(f"发票号码 {no} 已经登记过了，重复登记会造成重复报销")
    amount = _as_float(p.get("amount"))
    if amount is None:
        raise ToolError("还差发票金额（价税合计）")
    cat = pick_category(db, p.get("category"))
    return (
        {
            "invoice_no": no,
            "invoice_code": str(p.get("invoice_code") or "").strip() or None,
            "invoice_type": str(p.get("invoice_type") or "增值税电子普通发票")[:32],
            "amount": amount,
            "tax_rate": _as_float(p.get("tax_rate")) or 0.0,
            "tax_amount": _as_float(p.get("tax_amount")) or 0.0,
            "invoice_date": (_as_date(p.get("invoice_date")) or date.today()).isoformat(),
            "seller_name": str(p.get("seller_name") or "").strip()[:128] or None,
            "seller_tax_no": str(p.get("seller_tax_no") or "").strip()[:32] or None,
            "buyer_name": str(p.get("buyer_name") or "").strip()[:128] or None,
            "category_id": cat.id if cat else None,
            "remark": str(p.get("remark") or "").strip()[:200] or None,
        },
        [f"价税合计 {amount:,.2f} 元，登记后状态为「未查验」"],
    )


@write_action(
    "update_invoice", "修改发票", "发票", roles=STAFF,
    params={
        "invoice": "必填，发票 id 或号码",
        "invoice_no": "str 选填", "invoice_type": "str 选填", "amount": "number 选填",
        "tax_rate": "number 选填", "tax_amount": "number 选填",
        "invoice_date": "YYYY-MM-DD 选填", "seller_name": "str 选填",
        "buyer_name": "str 选填", "category": "str 选填", "remark": "str 选填",
    },
    note="识别错的字段用它修正",
)
def p_update_invoice(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = _scope_invoice(ctx, p.get("invoice") or p.get("invoice_id"))
    cat = pick_category(ctx.db, p.get("category")) if p.get("category") else obj.category
    body = {
        "id": obj.id,
        "invoice_no": str(p.get("invoice_no") or obj.invoice_no),
        "invoice_code": obj.invoice_code,
        "invoice_type": str(p.get("invoice_type") or obj.invoice_type),
        "amount": _as_float(p.get("amount")) if _as_float(p.get("amount")) is not None else ser.money(obj.amount),
        "tax_rate": _as_float(p.get("tax_rate")) if _as_float(p.get("tax_rate")) is not None else ser.money(obj.tax_rate),
        "tax_amount": _as_float(p.get("tax_amount")) if _as_float(p.get("tax_amount")) is not None else ser.money(obj.tax_amount),
        "invoice_date": (_as_date(p.get("invoice_date")) or obj.invoice_date).isoformat()
        if (_as_date(p.get("invoice_date")) or obj.invoice_date) else None,
        "seller_name": str(p.get("seller_name") or obj.seller_name or "") or None,
        "seller_tax_no": obj.seller_tax_no,
        "buyer_name": str(p.get("buyer_name") or obj.buyer_name or "") or None,
        "buyer_tax_no": obj.buyer_tax_no,
        "category_id": cat.id if cat else None,
        "reimbursement_id": obj.reimbursement_id,
        "item_id": obj.item_id,
        "remark": p.get("remark") if p.get("remark") is not None else obj.remark,
    }
    return body, [f"改的是发票 {obj.invoice_no}（当前 {ser.money(obj.amount):,.2f} 元）"]


@write_action(
    "categorize_invoices", "设置发票费用类型", "发票", roles=STAFF,
    params={"invoices": "必填，发票 id 或号码的数组", "category": "必填，费用类型名或 id"},
)
def p_categorize(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    raw = p.get("invoices") or p.get("invoice_ids")
    if not isinstance(raw, list) or not raw:
        raise ToolError("请指出要给哪几张发票设置费用类型")
    rows = [_scope_invoice(ctx, v) for v in raw]
    cat = pick_category(ctx.db, _need(p, "category", "费用类型"))
    if cat is None:
        raise ToolError("费用类型没找到，可以先到「费用类型」页创建")
    return (
        {"invoice_ids": [r.id for r in rows], "category_id": cat.id, "category_name": cat.name},
        [f"{len(rows)} 张发票将归到「{cat.name}」"],
    )


@write_action(
    "link_invoices", "把发票关联到报销单", "发票", roles=STAFF,
    params={"reimbursement": "必填，单号或 id", "invoices": "必填，发票 id 或号码数组"},
)
def p_link(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = _scope_reimbursement(ctx, p.get("reimbursement") or p.get("reimbursement_id"))
    raw = p.get("invoices") or p.get("invoice_ids")
    if not isinstance(raw, list) or not raw:
        raise ToolError("请指出要关联哪几张发票")
    rows = [_scope_invoice(ctx, v) for v in raw]
    warns = [f"关联到 {obj.code}（{obj.status}）"]
    already = [r for r in rows if r.reimbursement_id == obj.id]
    if already:
        warns.append(f"其中 {len(already)} 张本来就已经挂在这张单上")
    if obj.status not in (m.ST_DRAFT, m.ST_REJECTED):
        warns.append(f"注意：单据当前是「{obj.status}」，关联后可能需要重新审批")
    return ({"reimbursement_id": obj.id, "code": obj.code, "invoice_ids": [r.id for r in rows]}, warns)


@write_action(
    "unlink_invoices", "把发票从报销单上取下", "发票", roles=STAFF,
    params={"invoices": "必填，发票 id 或号码数组"},
)
def p_unlink(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    raw = p.get("invoices") or p.get("invoice_ids")
    if not isinstance(raw, list) or not raw:
        raise ToolError("请指出要取下哪几张发票")
    rows = [_scope_invoice(ctx, v) for v in raw]
    loose = [r for r in rows if r.reimbursement_id is None]
    warns = []
    if loose:
        warns.append(f"其中 {len(loose)} 张本来就没挂在任何单上")
    return ({"invoice_ids": [r.id for r in rows]}, warns)


@write_action(
    "check_invoices", "查验发票", "发票", roles=STAFF,
    params={"invoices": "选填，发票 id 或号码数组；不给则查验全部未查验的"},
)
def p_check(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    raw = p.get("invoices") or p.get("invoice_ids")
    if raw:
        rows = [_scope_invoice(ctx, v) for v in (raw if isinstance(raw, list) else [raw])]
        ids = [r.id for r in rows]
        warns = [f"将查验 {len(ids)} 张指定发票"]
    else:
        ids = []
        warns = ["未指定发票，将查验全部「未查验」的发票"]
    return ({"invoice_ids": ids}, warns)


@write_action(
    "delete_invoice", "删除发票", "发票", roles=STAFF, danger=True,
    params={"invoice": "必填，发票 id 或号码"},
    note="连同影像一起删除，且不可恢复",
)
def p_delete_invoice(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = _scope_invoice(ctx, p.get("invoice") or p.get("invoice_id"))
    return (
        {"invoice_id": obj.id, "invoice_no": obj.invoice_no},
        [f"将删除发票 {obj.invoice_no}（{ser.money(obj.amount):,.2f} 元），不可恢复"],
    )


# ---------------------------------------------------------------- 主数据


@write_action(
    "create_customer", "新建客户", "主数据", roles=ADMIN_ONLY,
    params={"name": "str 必填", "code": "str 选填", "contact": "str 选填（联系人）",
            "phone": "str 选填", "industry": "str 选填", "remark": "str 选填"},
)
def p_create_customer(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    name = str(_need(p, "name", "客户名称")).strip()
    dup = ctx.db.query(m.Customer).filter(m.Customer.name == name).first()
    if dup:
        raise ToolError(f"客户「{name}」已经存在（id={dup.id}），直接用它就行")
    return ({"name": name[:128], "code": str(p.get("code") or "").strip()[:32] or None,
             "contact": p.get("contact"), "phone": p.get("phone"),
             "industry": p.get("industry"), "remark": p.get("remark")}, [])


@write_action(
    "update_customer", "修改客户", "主数据", roles=ADMIN_ONLY,
    params={"customer": "必填，客户名或 id", "name": "str 选填", "code": "str 选填",
            "contact": "str 选填", "phone": "str 选填", "industry": "str 选填", "remark": "str 选填"},
)
def p_update_customer(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = pick_customer(ctx.db, _need(p, "customer", "客户"))
    body = {"id": obj.id, "name": str(p.get("name") or obj.name),
            "code": p.get("code") or obj.code, "contact": p.get("contact") or obj.contact,
            "phone": p.get("phone") or obj.phone, "industry": p.get("industry") or obj.industry,
            "remark": p.get("remark") if p.get("remark") is not None else obj.remark}
    return body, [f"改的是客户 {obj.name}(id={obj.id})"]


@write_action(
    "create_project", "新建项目", "主数据", roles=ADMIN_ONLY,
    params={"name": "str 必填", "customer": "str 选填，客户名或 id", "code": "str 选填",
            "manager": "str 选填", "stage": "str 选填", "status": "str 选填，默认进行中"},
)
def p_create_project(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    name = str(_need(p, "name", "项目名称")).strip()
    if ctx.db.query(m.Project).filter(m.Project.name == name).first():
        raise ToolError(f"项目「{name}」已经存在")
    cust = pick_customer(ctx.db, p.get("customer")) if p.get("customer") else None
    return ({"name": name[:128], "code": str(p.get("code") or "").strip()[:32] or None,
             "customer_id": cust.id if cust else None, "manager": p.get("manager"),
             "stage": p.get("stage"), "status": str(p.get("status") or "进行中")}, [])


@write_action(
    "update_project", "修改项目", "主数据", roles=ADMIN_ONLY,
    params={"project": "必填，项目名或 id", "name": "str 选填", "customer": "str 选填",
            "code": "str 选填", "manager": "str 选填", "stage": "str 选填", "status": "str 选填"},
)
def p_update_project(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = pick_project(ctx.db, _need(p, "project", "项目"))
    cust = pick_customer(ctx.db, p["customer"]) if p.get("customer") else obj.customer
    return ({"id": obj.id, "name": str(p.get("name") or obj.name), "code": p.get("code") or obj.code,
             "customer_id": cust.id if cust else None, "manager": p.get("manager") or obj.manager,
             "stage": p.get("stage") or obj.stage, "status": str(p.get("status") or obj.status)},
            [f"改的是项目 {obj.name}" ])


@write_action(
    "create_department", "新建部门", "主数据", roles=ADMIN_ONLY,
    params={"name": "str 必填", "code": "str 选填", "manager": "str 选填", "remark": "str 选填"},
)
def p_create_department(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    name = str(_need(p, "name", "部门名称")).strip()
    if ctx.db.query(m.Department).filter(m.Department.name == name).first():
        raise ToolError(f"部门「{name}」已经存在")
    return ({"name": name[:64], "code": str(p.get("code") or "").strip()[:32] or None,
             "manager": p.get("manager"), "remark": p.get("remark")}, [])


@write_action(
    "update_department", "修改部门", "主数据", roles=ADMIN_ONLY,
    params={"department": "必填，部门名或 id", "name": "str 选填", "code": "str 选填",
            "manager": "str 选填", "remark": "str 选填"},
)
def p_update_department(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = pick_department(ctx.db, _need(p, "department", "部门"))
    return ({"id": obj.id, "name": str(p.get("name") or obj.name), "code": p.get("code") or obj.code,
             "manager": p.get("manager") or obj.manager,
             "remark": p.get("remark") if p.get("remark") is not None else obj.remark},
            [f"改的是部门 {obj.name}"])


def _next_employee_no(db: Session) -> str:
    n = db.query(func.count(m.Employee.id)).scalar() or 0
    while True:
        n += 1
        code = f"E{n:04d}"
        if not db.query(m.Employee).filter(m.Employee.employee_no == code).first():
            return code


@write_action(
    "create_employee", "新建员工", "主数据", roles=ADMIN_ONLY,
    params={"name": "str 必填", "employee_no": "str 选填，不给会自动分配",
            "department": "str 选填，部门名或 id", "position": "str 选填",
            "email": "str 选填", "phone": "str 选填"},
)
def p_create_employee(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    db = ctx.db
    name = str(_need(p, "name", "员工姓名")).strip()
    dept = pick_department(db, p.get("department")) if p.get("department") else None
    warns = []
    no = str(p.get("employee_no") or "").strip()
    if not no:
        no = _next_employee_no(db)
        warns.append(f"未给工号，自动分配为 {no}")
    if db.query(m.Employee).filter(m.Employee.employee_no == no).first():
        raise ToolError(f"工号 {no} 已存在")
    if dept is None:
        same = db.query(m.Employee).filter(m.Employee.name == name).count()
        if same and not dept:
            warns.append("同名员工已存在，注意区分")
    return ({"name": name[:32], "employee_no": no[:32], "department_id": dept.id if dept else None,
             "position": p.get("position"), "level": None, "email": p.get("email"),
             "phone": p.get("phone"), "bank_account": None, "active": True}, warns)


@write_action(
    "update_employee", "修改员工", "主数据", roles=ADMIN_ONLY,
    params={"employee": "必填，姓名/工号/id", "name": "str 选填", "employee_no": "str 选填",
            "department": "str 选填", "position": "str 选填", "email": "str 选填",
            "phone": "str 选填", "active": "bool 选填，false=停用"},
)
def p_update_employee(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = pick_employee(ctx.db, _need(p, "employee", "员工"))
    dept = pick_department(ctx.db, p["department"]) if p.get("department") else obj.department
    active = p.get("active")
    body = {"id": obj.id, "name": str(p.get("name") or obj.name),
            "employee_no": str(p.get("employee_no") or obj.employee_no),
            "department_id": dept.id if dept else None, "position": p.get("position") or obj.position,
            "level": obj.level, "email": p.get("email") or obj.email,
            "phone": p.get("phone") or obj.phone, "bank_account": obj.bank_account,
            "active": bool(obj.active if active is None else active)}
    return body, [f"改的是员工 {obj.name}（工号 {obj.employee_no}）"]


@write_action(
    "create_category", "新建费用类型", "主数据", roles=STAFF,
    params={"name": "str 必填", "group_name": "str 选填，分组",
            "requires_invoice": "bool 选填，默认 true", "single_limit": "number 选填，单笔限额，0=不限",
            "code": "str 选填", "remark": "str 选填"},
)
def p_create_category(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    name = str(_need(p, "name", "费用类型名称")).strip()
    if ctx.db.query(m.ExpenseCategory).filter(m.ExpenseCategory.name == name).first():
        raise ToolError(f"费用类型「{name}」已经存在")
    req_inv = p.get("requires_invoice")
    return ({"name": name[:64], "code": str(p.get("code") or "").strip()[:32] or None,
             "group_name": p.get("group_name"), "requires_invoice": True if req_inv is None else bool(req_inv),
             "single_limit": _as_float(p.get("single_limit")) or 0.0,
             "daily_limit": 0.0, "monthly_limit": 0.0, "active": True, "remark": p.get("remark")}, [])


@write_action(
    "update_category", "修改费用类型", "主数据", roles=STAFF,
    params={"category": "必填，类型名或 id", "name": "str 选填", "group_name": "str 选填",
            "requires_invoice": "bool 选填", "single_limit": "number 选填", "active": "bool 选填"},
)
def p_update_category(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = pick_category(ctx.db, _need(p, "category", "费用类型"))
    def _bool(v, cur):
        return cur if v is None else bool(v)

    return ({"id": obj.id, "name": str(p.get("name") or obj.name), "code": obj.code,
             "group_name": str(p.get("group_name") or obj.group_name or "") or None,
             "requires_invoice": _bool(p.get("requires_invoice"), obj.requires_invoice),
             "single_limit": _as_float(p.get("single_limit")) if _as_float(p.get("single_limit")) is not None
             else ser.money(obj.single_limit),
             "daily_limit": ser.money(obj.daily_limit), "monthly_limit": ser.money(obj.monthly_limit),
             "active": _bool(p.get("active"), obj.active),
             "remark": obj.remark}, [f"改的是费用类型 {obj.name}"])


@write_action(
    "set_budget", "设置预算", "主数据", roles=STAFF,
    params={"year": "int 必填", "amount": "number 必填",
            "month": "int 选填（不填=全年预算）", "department": "str 选填，部门名或 id",
            "project": "str 选填", "category": "str 选填，费用类型名或 id", "remark": "str 选填"},
    note="同一维度组合的预算已存在时会改为更新",
)
def p_set_budget(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    db = ctx.db
    year = _as_int(p.get("year")) or date.today().year
    amount = _as_float(p.get("amount"))
    if amount is None:
        raise ToolError("还差预算金额")
    month = _as_int(p.get("month"))
    dept = pick_department(db, p.get("department")) if p.get("department") else None
    proj = pick_project(db, p.get("project")) if p.get("project") else None
    cat = pick_category(db, p.get("category")) if p.get("category") else None
    q = db.query(m.Budget).filter(m.Budget.year == year)
    q = q.filter(m.Budget.month.is_(None) if month is None else m.Budget.month == month)
    q = q.filter(m.Budget.department_id.is_(None) if dept is None else m.Budget.department_id == dept.id)
    q = q.filter(m.Budget.project_id.is_(None) if proj is None else m.Budget.project_id == proj.id)
    q = q.filter(m.Budget.category_id.is_(None) if cat is None else m.Budget.category_id == cat.id)
    exists = q.first()
    dims = "、".join(x for x in [
        f"{year} 年" + (f"{month} 月" if month else "全年"),
        f"部门 {dept.name}" if dept else "", f"项目 {proj.name}" if proj else "",
        f"费用类型 {cat.name}" if cat else "",
    ] if x)
    warns = [f"维度：{dims or '公司整体'}"]
    if exists:
        warns.append(f"该维度已有预算（{ser.money(exists.amount):,.2f} 元），本次是更新")
    return ({"id": exists.id if exists else None, "year": year, "month": month,
             "department_id": dept.id if dept else None, "project_id": proj.id if proj else None,
             "category_id": cat.id if cat else None, "amount": amount,
             "remark": p.get("remark")}, warns)


_MASTER_DELETE = {
    "customers": (m.Customer, "客户", "name", ADMIN_ONLY),
    "projects": (m.Project, "项目", "name", ADMIN_ONLY),
    "departments": (m.Department, "部门", "name", ADMIN_ONLY),
    "employees": (m.Employee, "员工", "name", ADMIN_ONLY),
    "categories": (m.ExpenseCategory, "费用类型", "name", STAFF),
}


@write_action(
    "delete_master", "删除主数据", "主数据", roles=STAFF, danger=True,
    params={"entity": "必填：customers|projects|departments|employees|categories",
            "name": "必填，名称或 id"},
    note="被业务数据引用的记录删不掉，系统会说明原因",
)
def p_delete_master(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    entity = str(p.get("entity") or "").strip().lower()
    meta = _MASTER_DELETE.get(entity)
    if not meta:
        raise ToolError("entity 只能是 customers/projects/departments/employees/categories")
    model, label, field, roles = meta
    if ctx.user.role not in roles:
        raise ToolError(f"当前角色「{ctx.user.role}」不能删除{label}")
    obj = _pick(ctx.db, model, _need(p, "name", f"{label}名称"), label, (field, "code"))
    if obj is None:
        raise ToolError(f"{label}没找到")
    return ({"entity": entity, "id": obj.id, "name": getattr(obj, field)},
            [f"将删除{label}「{getattr(obj, field)}」；若已被业务数据引用，接口会拒绝并说明原因"])


# ---------------------------------------------------------------- 账号 / 参数


def _gen_password() -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(9)) + "@1"


@write_action(
    "create_user", "新建登录账号", "账号", roles=ADMIN_ONLY,
    params={"username": "str 必填，登录名", "name": "str 必填，显示名",
            "role": "str 必填：申请人|审批人|财务|管理员", "password": "str 选填，不给会随机生成",
            "employee": "str 选填，绑定员工姓名或 id", "approval_level": "int 选填，审批人用（1/2）"},
)
def p_create_user(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    db = ctx.db
    username = str(_need(p, "username", "登录名")).strip()
    name = str(_need(p, "name", "显示名")).strip()
    role = str(p.get("role") or "").strip()
    if role not in m.ROLES:
        raise ToolError(f"角色只能是 {'/'.join(m.ROLES)} 之一")
    if db.query(m.AppUser).filter(m.AppUser.username == username).first():
        raise ToolError(f"登录名 {username} 已被占用")
    emp = pick_employee(db, p.get("employee")) if p.get("employee") else None
    level = _as_int(p.get("approval_level")) or (1 if role == m.ROLE_APPROVER else 0)
    pwd = str(p.get("password") or "").strip()
    warns = []
    if not pwd:
        pwd = _gen_password()
        warns.append(f"初始口令：{pwd}（首次登录会强制修改）")
    if role == m.ROLE_APPROVER and not emp:
        warns.append("审批人没绑定员工，审批范围会算不出来，建议补上")
    return ({"username": username[:64], "name": name[:32], "role": role, "password": pwd,
             "employee_id": emp.id if emp else None, "approval_level": min(max(level, 0), 2),
             "active": True}, warns)


@write_action(
    "update_user", "修改账号", "账号", roles=ADMIN_ONLY,
    params={"user": "必填，登录名或 id", "name": "str 选填", "role": "str 选填",
            "employee": "str 选填", "approval_level": "int 选填", "active": "bool 选填（false=停用）"},
)
def p_update_user(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = pick_user(ctx.db, _need(p, "user", "账号"))
    warns = []
    if obj.role == m.ROLE_ADMIN and (p.get("active") is False or (p.get("role") and p["role"] != m.ROLE_ADMIN)):
        warns.append("这是管理员账号，系统会拒绝把最后一个可用管理员降级或停用")
    emp = pick_employee(ctx.db, p["employee"]) if p.get("employee") else (
        ctx.db.get(m.Employee, obj.employee_id) if obj.employee_id else None
    )
    body = {"id": obj.id, "name": str(p.get("name") or obj.name)}
    if p.get("role"):
        if p["role"] not in m.ROLES:
            raise ToolError(f"角色只能是 {'/'.join(m.ROLES)} 之一")
        body["role"] = p["role"]
    if p.get("employee") or emp:
        body["employee_id"] = emp.id if emp else None
    if p.get("approval_level") is not None:
        body["approval_level"] = min(max(_as_int(p.get("approval_level")) or 0, 0), 2)
    if p.get("active") is not None:
        body["active"] = bool(p.get("active"))
    return body, warns + [f"改的是账号 {obj.username}（{obj.role}）"]


@write_action(
    "reset_password", "重置账号口令", "账号", roles=ADMIN_ONLY, danger=True,
    params={"user": "必填，登录名或 id", "password": "str 选填，不给会随机生成"},
)
def p_reset_password(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    obj = pick_user(ctx.db, _need(p, "user", "账号"))
    pwd = str(p.get("password") or "").strip()
    warns = []
    if not pwd:
        pwd = _gen_password()
        warns.append(f"新口令：{pwd}（已设为首次登录强制修改）")
    warns.append(f"{obj.username} 的登录状态会全部失效，需要重新登录")
    return ({"id": obj.id, "username": obj.username, "new_password": pwd}, warns)


@write_action(
    "set_setting", "修改系统参数", "账号", roles=ADMIN_ONLY,
    params={"key": "必填，参数名", "value": "必填，参数值"},
    note="只允许改系统预定义的参数",
)
def p_set_setting(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    from . import approvals as ap

    key = str(_need(p, "key", "参数名")).strip()
    value = str(_need(p, "value", "参数值")).strip()
    if key not in ap.DEFAULT_SETTINGS:
        raise ToolError(
            "参数名只能是：" + "、".join(sorted(ap.DEFAULT_SETTINGS))
        )
    old = ap.get_setting(ctx.db, key)
    return ({"key": key, "value": value}, [f"{key}：{old} → {value}"])


# ---------------------------------------------------------------- 收票邮箱


@write_action(
    "create_mail_account", "新增收票邮箱", "收件箱", roles=ADMIN_ONLY,
    params={"name": "str 必填，展示名", "host": "str 必填，IMAP 服务器",
            "username": "str 必填，邮箱账号", "password": "str 必填，授权码",
            "port": "int 选填，SSL 默认 993", "use_ssl": "bool 选填，默认 true",
            "folder": "str 选填，默认 INBOX", "subject_keywords": "str 选填，主题关键词，逗号分隔"},
    note="口令用主密钥加密落库，接口只回显掩码",
)
def p_create_mail(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    return ({"name": str(_need(p, "name", "展示名")).strip(),
             "host": str(_need(p, "host", "IMAP 服务器")).strip(),
             "username": str(_need(p, "username", "邮箱账号")).strip(),
             "password": str(_need(p, "password", "授权码")),
             "port": _as_int(p.get("port")) or 993,
             "use_ssl": True if p.get("use_ssl") is None else bool(p.get("use_ssl")),
             "folder": str(p.get("folder") or "INBOX"),
             "subject_keywords": p.get("subject_keywords"),
             "active": True},
            ["建议保存后先用「测试连接」确认服务器与授权码可用"])


@write_action(
    "sync_mail", "立即收票", "收件箱", roles=STAFF,
    params={"account": "str 选填，邮箱展示名或 id；不给则全部启用账号都收"},
)
def p_sync_mail(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    if p.get("account"):
        acc = _pick(ctx.db, m.MailAccount, p["account"], "收票邮箱", ("name", "username"))
        return ({"account_id": acc.id if acc else None}, [f"只收「{acc.name}」这个邮箱"])
    n = ctx.db.query(m.MailAccount).filter(m.MailAccount.active.is_(True)).count()
    if not n:
        raise ToolError("还没有启用的收票邮箱，先到「发票收件箱」里添加")
    return ({"account_id": None}, [f"将收取全部 {n} 个启用邮箱"])


# ---------------------------------------------------------------- 批量


@write_action(
    "batch_check_invoices", "批量查验发票", "发票", roles=STAFF,
    params={"invoices": "选填，发票 id 数组；不给则查验全部未查验的"},
)
def p_batch_check(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    raw = p.get("invoices") or p.get("invoice_ids")
    if raw:
        rows = [_scope_invoice(ctx, v) for v in (raw if isinstance(raw, list) else [raw])]
        ids = [r.id for r in rows]
        return ({"invoice_ids": ids}, [f"将查验 {len(ids)} 张"])
    return ({"invoice_ids": []}, ["未指定发票，查验全部未查验的"])


@write_action(
    "batch_delete", "批量删除", "批量", roles=STAFF, danger=True,
    params={"entity": "必填：invoices|reimbursements", "ids": "必填，id 数组"},
    note="被引用的记录会被跳过并说明原因",
)
def p_batch_delete(ctx: Ctx, p: dict) -> tuple[dict, list[str]]:
    entity = str(p.get("entity") or "").strip().lower()
    if entity not in ("invoices", "reimbursements"):
        raise ToolError("entity 只能是 invoices / reimbursements")
    ids = [n for n in (_as_int(i) for i in (p.get("ids") or [])) if n]
    if not ids:
        raise ToolError("请给出要删除的 id 列表")
    if len(ids) > 100:
        raise ToolError("一次最多删 100 条，请分批")
    if entity == "invoices":
        rows = [_scope_invoice(ctx, i) for i in ids]
        ids = [r.id for r in rows]
    else:
        rows = [_scope_reimbursement(ctx, i) for i in ids]
        ids = [r.id for r in rows]
    return ({"entity": entity, "ids": ids}, [f"将删除 {len(ids)} 条记录，不可恢复"])


# ================================================================== 对外接口


def actions_for(user) -> list[dict]:
    return [a for a in WRITE_ACTIONS.values() if user.role in a["roles"]]


def find_action(type_: str):
    return WRITE_ACTIONS.get(str(type_ or "").strip())


def prepare_write(ctx: Ctx, type_: str, params: dict) -> tuple[dict, list[str], dict]:
    """校验并规范化一个写动作。返回 (params, warnings, meta)。"""
    action = find_action(type_)
    if action is None:
        raise ToolError(f"没有名为「{type_}」的操作")
    if ctx.user.role not in action["roles"]:
        raise ToolError(f"当前角色「{ctx.user.role}」不能执行「{action['label']}」")
    clean, warns = action["prepare"](ctx, dict(params or {}))
    meta = {"type": action["type"], "label": action["label"], "danger": action["danger"],
            "note": action["note"]}
    return clean, list(warns or []), meta


def _params_doc(params: dict) -> str:
    return json.dumps(params, ensure_ascii=False)


def catalog_text(user) -> str:
    """给模型看的「只读工具 + 写动作」目录（按角色过滤）。"""
    lines: list[str] = ["### 只读查询工具（用 ```tool 调用，服务端直接查库返回）"]
    for t in tools_for(user):
        lines.append(f"- {t['name']}（{t['label']}）：{t['desc']}\n  参数 {_params_doc(t['params'])}")
    lines.append("")
    lines.append("### 可起草的写操作（用 ```action 调用，用户确认后由前端执行）")
    for a in actions_for(user):
        tail = f"　[{a['note']}]" if a["note"] else ""
        mark = "（不可逆，先提醒用户）" if a["danger"] else ""
        lines.append(f"- {a['type']}（{a['label']}）{mark}：参数 {_params_doc(a['params'])}{tail}")
    return "\n".join(lines)


def action_labels() -> dict:
    return {a["type"]: a["label"] for a in WRITE_ACTIONS.values()}


# 模型回复里允许出现的动作类型全集（`parse_action` 用它挡掉不存在的动作名）。
# 放在文件末尾：注册表在上面才填满，提前取值会是个空集合。
ASSISTANT_ACTION_TYPES: frozenset[str] = frozenset(WRITE_ACTIONS)


def catalog_for(user) -> str:
    """按角色过滤后的完整能力目录（工具 + 动作）。"""
    return catalog_text(user)


# ============================================================================
# Jev 驱动的快速决策工具（v2.7.9+）
# ----------------------------------------------------------------------------
# Jev（TypeSafe AI System One）不写文本，只做三种结构化决策：
#   boolean（真值概率）/ choice（多选一）/ score（分段打分）。
# 优势：单次 ~0.4s，$0.00004/decision，比大模型快 200× 便宜 400×。
# 协议细节与坑位见 app/jev_client.py 模块头部。
# 未配置 key → source='unavailable'；调用失败 → source='error'；成功 → source='jev'。
# 后两种情况都由上层退回大模型或规则，不影响功能可用性。
# ============================================================================

def _jev_unavailable(what: str, **extra) -> dict:
    """Jev 调用失败时的统一返回。

    关键：source 必须是 'error' 而不是 'jev' —— 否则上层会把「调用挂了」当成模型给出的
    结论（早期版本就这么干过：分类失败仍回 source='jev' + category=None）。
    """
    err = (jev_client.last_error() or {}).get("message", "")
    out = {
        "source": "error",
        "note": f"Jev {what}调用失败，请改用其他方式判断"
                + (f"（{err}）" if err else ""),
    }
    out.update(extra)
    return out


JEV_OPTIONS_CATEGORY = [
    "业务招待费", "交通费", "办公用品", "差旅费", "通讯费",
    "会议费", "培训费", "其他费用",
]
JEV_OPTIONS_ANOMALY = ["正常", "轻微异常", "异常"]


@read_tool(
    name="jev_classify_category",
    label="Jev 快速分类发票类别",
    desc=(
        "用 Jev 模型（~0.4s）快速判断发票属于哪个费用类型。"
        "返回 {category, confidence, source}；source='jev' 为模型结论，"
        "'unavailable' 未配置 key，'error' 调用失败（此时勿采信 category）。"
    ),
    roles=ALL_ROLES,
    params={
        "description": {"type": "string", "desc": "发票描述（如\"晚餐招待客户\"）"},
        "amount": {"type": "number", "desc": "金额（元），用于辅助分类"},
    },
)
def t_jev_classify_category(ctx: Ctx, p: dict) -> dict:
    desc = _as_str(p.get("description")) or ""
    amount = _as_float(p.get("amount"))
    if not desc:
        raise ToolError("description 必填")
    if not jev_client.jev_available(ctx.db):
        return {"source": "unavailable", "note": "JEV_API_KEY 未配置"}
    question = f"以下发票属于哪个费用类型？（金额 {amount or '?'} 元）"
    ans, conf = jev_client.jev_choice(
        question, JEV_OPTIONS_CATEGORY, context=desc[:500], db=ctx.db
    )
    if not ans:
        # 别把失败伪装成 jev 的结论：调用挂了就如实说，让上层退回大模型/规则
        return _jev_unavailable("分类", category=None, confidence=0.0)
    return {
        "category": ans,
        "confidence": round(conf, 3) if conf else 0.0,
        "source": "jev",
    }


@read_tool(
    name="jev_score_anomaly",
    label="Jev 异常打分（1-5）",
    desc=(
        "用 Jev 给报销单打异常分（1=完全正常，5=高度可疑）。"
        "返回 {score, source}；未配置或调用失败时 score=-1。"
    ),
    roles=ALL_ROLES,
    params={
        "description": {"type": "string", "desc": "报销单描述"},
        "amount": {"type": "number", "desc": "金额（元）"},
        "submitter_history": {"type": "string", "desc": "提交人近期同类报销摘要（可选）"},
    },
)
def t_jev_score_anomaly(ctx: Ctx, p: dict) -> dict:
    desc = _as_str(p.get("description")) or ""
    amount = _as_float(p.get("amount"))
    history = _as_str(p.get("submitter_history")) or ""
    if not desc:
        raise ToolError("description 必填")
    if not jev_client.jev_available(ctx.db):
        return {"source": "unavailable", "note": "JEV_API_KEY 未配置"}
    ctx_str = f"金额 {amount or '?'} 元。{history[:200]}" if history else f"金额 {amount or '?'} 元"
    question = "以下报销单有多可疑？1=完全正常，5=高度可疑（金额异常/重复/与业务无关等）"
    score = jev_client.jev_score(question, scale_min=1, scale_max=5, context=desc[:300] + " | " + ctx_str, db=ctx.db)
    if score < 0:
        return _jev_unavailable("异常打分", score=-1)
    return {
        "score": int(score),
        "source": "jev",
    }


@read_tool(
    name="jev_route_approval",
    label="Jev 审批路由建议",
    desc=(
        "用 Jev 根据金额 + 描述建议审批人。"
        "返回 {role, confidence, source}；source='error' 时 role 为 null，勿采信。"
    ),
    roles=ALL_ROLES,
    params={
        "amount": {"type": "number", "desc": "报销金额（元）"},
        "description": {"type": "string", "desc": "报销事由"},
    },
)
def t_jev_route_approval(ctx: Ctx, p: dict) -> dict:
    amount = _as_float(p.get("amount"))
    desc = _as_str(p.get("description")) or ""
    if amount is None:
        raise ToolError("amount 必填")
    if not jev_client.jev_available(ctx.db):
        return {"role": "财务", "confidence": 0, "source": "unavailable"}
    question = (
        f"金额 {amount} 元的报销（{desc[:100]}），应由哪一级审批？"
        f"<2000=财务直接审；2000~20k=部门负责人；>=20k=总经理"
    )
    ans, conf = jev_client.jev_choice(
        question, ["财务", "部门负责人", "总经理"], context=desc[:300], db=ctx.db
    )
    if not ans:
        return _jev_unavailable("审批路由", role=None, confidence=0.0)
    return {
        "role": ans,
        "confidence": round(conf, 3) if conf else 0.0,
        "source": "jev",
    }


@read_tool(
    name="jev_check_duplicate",
    label="Jev 检测重复报销",
    desc=(
        "用 Jev 判断当前发票与历史记录是否实质重复（不是字面相似）。"
        "返回 {is_duplicate, duplicate_probability, source}；"
        "source='error' 时 is_duplicate 为 null。"
    ),
    roles=ALL_ROLES,
    params={
        "current": {"type": "string", "desc": "当前发票描述/编号"},
        "history": {"type": "string", "desc": "历史相似报销描述/编号"},
    },
)
def t_jev_check_duplicate(ctx: Ctx, p: dict) -> dict:
    cur = _as_str(p.get("current")) or ""
    hist = _as_str(p.get("history")) or ""
    if not cur or not hist:
        raise ToolError("current 和 history 都必填")
    if not jev_client.jev_available(ctx.db):
        return {"is_duplicate": None, "source": "unavailable"}
    ans, conf = jev_client.jev_noul(
        "以下两笔报销是否实质重复（同一笔费用重复报）？",
        context=f"当前：{cur[:200]} | 历史：{hist[:200]}",
        db=ctx.db,
    )
    if ans < 0:
        return _jev_unavailable("重复检测", is_duplicate=None, duplicate_probability=-1)
    return {
        "is_duplicate": ans > 0.5,
        "duplicate_probability": round(ans, 3),
        "confidence": round(conf, 3) if conf else 0.0,
        "source": "jev",
    }
