"""AI 服务编排：把模型能力接到业务数据上。

分工
----
- `ai.py`      传输与配置
- `ai_tasks.py` 提示词与结构化解析（不碰数据库）
- `ai_service.py`（本文件）读库拼上下文、挑 provider、跑任务、记用量
- `routers/ai.py` HTTP 接口

上下文怎么给模型
----------------
助手要能回答「我有多少张待审批」「帮我把这 3 张票做成报销单」，就必须知道
**当前用户是谁、能看什么、有哪些 id 可选**。所以这里会按用户的可见范围拼一段
紧凑的文本上下文：用户信息 + 功能地图 + 主数据 id 清单 + 业务数据摘要。

刻意做了三件事：
1. **按权限取数**，不越权把全公司的数据塞进提示词；
2. **限量**（清单截断 + 条数上限），避免提示词过长既贵又慢；
3. **不传敏感字段**：不传密码哈希、不传会话、不传完整税号等与问答无关的内容。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import ai, ai_tasks, models as m
from . import approvals as ap

# 上下文里各类清单的条数上限
MAX_LIST = 40

FEATURE_ATTR = {
    "assistant": "use_assistant",
    "recognize": "use_recognize",
    "analyze": "use_analyze",
}


def provider_for(db: Session, feature: str = "assistant", provider_id: int | None = None):
    """取可用于某场景的 provider。

    管理员可以给每家单独关掉某个场景（比如把便宜的小模型只留给识别），
    所以这里除了「启用」还要看对应开关。
    """
    attr = FEATURE_ATTR.get(feature, "use_assistant")
    row = ai.resolve_provider(db, provider_id)
    if row is None:
        return None
    if not getattr(row, attr, True):
        # 指定了 provider 却关了这个场景，明确拒绝；否则再挑一个能用的
        if provider_id:
            return None
        row = (
            db.query(m.AiProvider)
            .filter(m.AiProvider.enabled.is_(True), getattr(m.AiProvider, attr).is_(True))
            .order_by(m.AiProvider.id.asc())
            .first()
        )
    return row


def ensure_provider(db: Session, feature: str = "assistant", provider_id: int | None = None) -> ai.AiConfig:
    row = provider_for(db, feature, provider_id)
    if row is None:
        raise ai.AiError("尚未配置可用的大模型。请让管理员到「AI 智能 → 模型配置」里添加并启用一个。")
    return ai.provider_to_config(row)


# ---------------------------------------------------------------- 发票识别增强

# 这四项是入账的刚需，缺任意一项都值得让大模型再看一眼
KEY_FIELDS = ("invoice_no", "amount", "invoice_date", "seller_name")


def _worth_ai(local: dict) -> bool:
    if not local.get("invoice_no"):
        return True
    missing = [k for k in KEY_FIELDS if not local.get(k)]
    if missing:
        return True
    # 识别的票面信息里购买方税号也常被漏掉，顺手让模型补
    return not local.get("buyer_tax_no")


# 这些是「本公司的购买方」信息，模型若把销售方当购买方填进来会很危险，
# 所以只接受模型在税号上的一致结果，不做名称猜测。
def enrich_invoice(
    db: Session,
    user,
    *,
    filename: str,
    data: bytes,
    local: dict,
    text: str = "",
    provider_id: int | None = None,
    force: bool = False,
) -> dict:
    """在本地识别结果之上，用大模型补齐缺失字段。

    返回 {fields, applied, info}。`applied` 只包含**本地没有**的字段——
    本地规则识别出来的值优先，模型只补空缺、不覆盖，避免把已经对的值改错。
    """
    row = provider_for(db, "recognize", provider_id)
    if row is None:
        return {"fields": {}, "applied": [], "info": {"used": False, "reason": "未配置可用的识别模型"}}
    if not force and not _worth_ai(local):
        return {"fields": {}, "applied": [], "info": {"used": False, "reason": "本地识别已覆盖关键字段"}}

    cfg = ai.provider_to_config(row)
    images: list[tuple[bytes, str]] = []
    if cfg.can_vision and data:
        images = _images_for_ai(data, filename)
    if not images and not text and not local.get("invoice_no"):
        return {"fields": {}, "applied": [], "info": {"used": False, "reason": "没有可送模型的文本或图片"}}

    hint_parts = []
    if local.get("invoice_no"):
        hint_parts.append(f"发票号码={local['invoice_no']}")
    if local.get("source"):
        hint_parts.append(f"本地识别来源={local['source']}")

    try:
        got = ai_tasks.fill_invoice(
            cfg, text=text, images=images or None, filename=filename,
            hint="；".join(hint_parts),
        )
    except ai.AiError as exc:
        ai.record_usage(db, user=user, kind=m.AI_RECOGNIZE, cfg=cfg, ok=False, error=str(exc),
                        detail=filename)
        return {"fields": {}, "applied": [], "info": {"used": False, "reason": str(exc), "model": cfg.model}}

    fields = got["fields"]
    applied = [k for k in fields if not k.startswith("_") and not local.get(k)]
    ai.record_usage(
        db, user=user, kind=m.AI_RECOGNIZE, cfg=cfg, ok=True, usage=got["usage"],
        detail=f"{filename} 补全 {','.join(applied) or '无'}",
    )
    return {
        "fields": {k: v for k, v in fields.items() if not k.startswith("_")},
        "applied": applied,
        "info": {
            "used": True,
            "model": got.get("model"),
            "provider": cfg.provider_name,
            "by_vision": bool(images),
            "confidence": fields.get("_confidence"),
            "note": fields.get("_note"),
            # 把补全清单也放进 info：界面要靠它显示「模型补了哪几项」。
            # 早先只返回在上一层的 applied 里，路由又用 `not ai_info.get("applied")` 判空，
            # 于是界面上永远显示「已调用但补全 0 项」——和事实完全相反。
            "applied": applied,
        },
    }


def _images_for_ai(data: bytes, filename: str) -> list[tuple[bytes, str]]:
    """准备送给视觉模型的图片。PDF / 图片都先转成 PNG，模型才认。"""
    lower = (filename or "").lower()
    if data[:4] == b"%PDF" or lower.endswith(".pdf"):
        pages = _ocr_module().pdf_to_images(data, max_pages=1, scale=1.6)
        return [(pages[0], "image/png")] if pages else []
    if len(data) > 6 * 1024 * 1024:
        return []  # 太大就不发给模型了，base64 之后更夸张
    return [(data, "")]


def _ocr_module():
    from . import ocr

    return ocr


# ---------------------------------------------------------------- 助手上下文


def _lines(title: str, rows: list[str]) -> str:
    if not rows:
        return ""
    head = f"{title}（共 {len(rows)} 条）"
    return head + "\n" + "\n".join(f"  - {r}" for r in rows[:MAX_LIST])


def build_context(db: Session, user, page: str = "", extra: dict | None = None) -> str:
    """拼出给助手的系统上下文。严格按用户可见范围取数。"""
    from . import security as sec

    today = date.today()
    parts: list[str] = [
        f"今天日期：{today.isoformat()}（{'一二三四五六日'[today.weekday()]}）",
        f"当前用户：{user.name}（账号 {user.username}，角色「{user.role}」"
        + (f"，审批级别 {user.approval_level} 级" if user.role == m.ROLE_APPROVER else "")
        + ")",
    ]
    # AppUser 上没有 department 关系，部门要从 employee.department_id 绕一道（见 models 里的
    # department_id property）。早先这里写的是 user.department.name，那个属性根本不存在，
    # 只要用户绑定了员工就直接 AttributeError → 500，助手对话全挂。
    dept_id = user.department_id
    if dept_id:
        dept = db.get(m.Department, dept_id)
        if dept:
            parts.append(f"所属部门：{dept.name}")
    if page:
        parts.append(f"用户当前所在页面：{page}")

    parts.append(
        "系统功能地图（左侧导航）：\n"
        "  - 统计看板：金额趋势、按费用类型/部门/客户/项目的分布、预算执行\n"
        "  - 报销单管理：新建、编辑草稿、提交、审批、驳回、付款、查看明细与审批流\n"
        "  - 发票管理：登记发票、上传识别、查验、关联到报销单、查看影像\n"
        "  - 发票收件箱：配置收票邮箱、立即收票、手工上传识别入账（权限：财务/管理员）\n"
        "  - 费用管理：费用类型、客户、项目、员工、部门、预算\n"
        "  - 异常与预警：重复报销、超限额、大额、超期未审批、问题发票\n"
        "  - 用户管理 / 系统参数 / 操作审计（管理员）\n"
        "  - AI 设置：模型厂商与 Key（管理员）"
    )
    parts.append(
        "注意：下面只是**开场摘要**，不是全量数据。要明细就调用只读工具去查"
        "（例如 list_alerts 看异常、list_reimbursements 筛单、list_master 找 id、"
        "search 按关键词全库找）。没查到再说没有。"
    )

    # ---- 主数据：动作参数要靠这些 id ----
    cats = db.query(m.ExpenseCategory).order_by(m.ExpenseCategory.id).all()
    if cats:
        parts.append(_lines(
            "费用类型可选值（id=名称）",
            [f"{c.id}={c.name}" + (f"（{c.group_name}）" if c.group_name else "") for c in cats],
        ))
    depts = db.query(m.Department).order_by(m.Department.id).all()
    if depts:
        parts.append(_lines("部门可选值（id=名称）", [f"{d.id}={d.name}" for d in depts]))
    # 员工清单是「新建报销单指定申请人」的刚需：
    # 早先上下文里没有员工，助手只能问用户要 applicant_id，最后生成的动作缺申请人，
    # 用户点确认才报「执行失败：请指定申请人」。
    emps = (
        db.query(m.Employee)
        .filter(m.Employee.active.is_(True))
        .order_by(m.Employee.id)
        .limit(MAX_LIST)
        .all()
    )
    if emps:
        dept_map = {d.id: d.name for d in depts}
        parts.append(_lines(
            "员工可选值（id=姓名 | 工号 | 部门）",
            [
                f"{e.id}={e.name} | {e.employee_no} | {dept_map.get(e.department_id or -1, '-')}"
                for e in emps
            ],
        ))
    customers = db.query(m.Customer).order_by(m.Customer.id.desc()).limit(MAX_LIST).all()
    if customers:
        parts.append(_lines("客户可选值（id=名称）", [f"{c.id}={c.name}" for c in customers]))
    projects = db.query(m.Project).order_by(m.Project.id.desc()).limit(MAX_LIST).all()
    if projects:
        parts.append(_lines("项目可选值（id=名称）", [f"{p.id}={p.name}" for p in projects]))
    if user.role in (m.ROLE_FINANCE, m.ROLE_ADMIN):
        parts.append(
            "审批与预算口径：超过 "
            f"{ap.get_float(db, 'approval_level2_threshold', 20000.0):,.0f} 元需二级审批；"
            f"大额预警线 {ap.get_float(db, 'large_amount_threshold', 20000.0):,.0f} 元；"
            f"待审批超期线 {ap.get_int(db, 'approval_overdue_days', 7)} 天"
        )

    # ---- 业务数据：按可见范围取，做汇总 ----
    conds = sec.scope_conds(user)
    q = db.query(m.Reimbursement)
    if conds:
        q = q.filter(*conds)
    counts = dict(
        (status, n)
        for status, n in q.with_entities(m.Reimbursement.status, func.count(m.Reimbursement.id))
        .group_by(m.Reimbursement.status)
        .all()
    )
    amount_sum = q.with_entities(func.coalesce(func.sum(m.Reimbursement.total_amount), 0)).scalar() or 0
    parts.append(
        "报销单概览（你可见的范围）：\n"
        + "\n".join(f"  - {s}：{counts.get(s, 0)} 张" for s in m.STATUS_FLOW if counts.get(s))
        + (f"\n  - 金额合计：{float(amount_sum):,.2f} 元" if amount_sum else "")
    )

    recent = (
        q.order_by(m.Reimbursement.id.desc()).limit(20).all()
    )
    if recent:
        parts.append(_lines(
            "最近的报销单（id=单号 | 标题 | 状态 | 金额 | 申请人）",
            [
                f"{r.id}={r.code} | {r.title} | {r.status} | {float(r.total_amount or 0):,.2f} | "
                f"{r.applicant.name if r.applicant else '-'}"
                for r in recent
            ],
        ))

    # 待我审批（审批人 / 管理员）
    if user.role == m.ROLE_APPROVER and user.department_id:
        pending = (
            db.query(m.Reimbursement)
            .filter(
                m.Reimbursement.department_id == user.department_id,
                m.Reimbursement.status == m.ST_PENDING,
                m.Reimbursement.approved_level < m.Reimbursement.required_level,
            )
            .order_by(m.Reimbursement.id.desc())
            .limit(20)
            .all()
        )
        if pending:
            parts.append(_lines(
                "待你审批的报销单（id=单号 | 标题 | 金额 | 当前待批级别）",
                [
                    f"{r.id}={r.code} | {r.title} | {float(r.total_amount or 0):,.2f} | "
                    f"第 {r.approved_level + 1} 级"
                    for r in pending
                ],
            ))

    # 发票：未关联的散票是「AI 记账」的原料
    inv_conds = sec.invoice_scope_conds(user)
    iq = db.query(m.Invoice)
    if inv_conds:
        iq = iq.filter(*inv_conds)
    inv_total = iq.count()
    unlinked = iq.filter(m.Invoice.reimbursement_id.is_(None)).count()
    parts.append(f"发票：共 {inv_total} 张，其中 {unlinked} 张未关联报销单（散票）")
    loose = (
        iq.filter(m.Invoice.reimbursement_id.is_(None))
        .order_by(m.Invoice.id.desc())
        .limit(30)
        .all()
    )
    if loose:
        parts.append(_lines(
            "未关联报销单的发票（id=号码 | 开票日期 | 销售方 | 价税合计 | 当前费用类型）",
            [
                f"{v.id}={v.invoice_no} | {v.invoice_date or '-'} | {v.seller_name or '-'} | "
                f"{float(v.amount or 0):,.2f} | {v.category.name if v.category else '未分类'}"
                for v in loose
            ],
        ))

    # 当前页面选中的对象（前端传入）
    if extra:
        focused = {k: v for k, v in extra.items() if v not in (None, "", [], {})}
        if focused:
            import json

            parts.append("用户界面上当前聚焦的内容：\n" + json.dumps(focused, ensure_ascii=False, default=str)[:3000])

    parts.append(
        "重要：上面清单里没有出现的 id 一律视为不存在，不要凭空编造 id。"
        "动作参数只能用上面给出的 id。"
    )
    return "\n\n".join(parts)


def system_prompt(db: Session, user, page: str = "", extra: dict | None = None) -> str:
    """助手系统提示词 = 角色化的能力目录（工具 + 动作）+ 本次会话上下文。"""
    from . import ai_tools

    return ai_tasks.assistant_system(build_context(db, user, page, extra), ai_tools.catalog_text(user))


# ---------------------------------------------------------------- 单据上下文


def reimbursement_payload(r: m.Reimbursement) -> dict:
    """把报销单压成给模型看的结构化摘要（不含任何敏感信息）。"""
    invoices = []
    for it in r.items:
        for v in it.invoices:
            invoices.append({
                "invoice_no": v.invoice_no,
                "amount": float(v.amount or 0),
                "seller": v.seller_name,
                "date": v.invoice_date.isoformat() if v.invoice_date else None,
                "check_status": v.check_status,
                "item": it.description,
                "category": it.category.name if it.category else None,
            })
    return {
        "编号": r.code,
        "标题": r.title,
        "事由": r.purpose,
        "状态": r.status,
        "申请人": r.applicant.name if r.applicant else None,
        "部门": r.department.name if r.department else None,
        "客户": r.customer.name if r.customer else None,
        "项目": r.project.name if r.project else None,
        "金额": float(r.total_amount or 0),
        # 报销单本身没有 tax_amount 字段——税额只落在明细行与发票上，
        # 这里按明细汇总，别去读那个不存在的属性（会直接 500）。
        "税额": round(sum(float(it.tax_amount or 0) for it in r.items), 2),
        "费用起止": [
            r.occur_start.isoformat() if r.occur_start else None,
            r.occur_end.isoformat() if r.occur_end else None,
        ],
        "提交时间": r.submit_at.isoformat(sep=" ", timespec="seconds") if r.submit_at else None,
        "需要审批级数": r.required_level,
        "已通过级数": r.approved_level,
        "明细": [
            {
                "费用类型": it.category.name if it.category else None,
                "发生日期": it.occur_date.isoformat() if it.occur_date else None,
                "金额": float(it.amount or 0),
                "税额": float(it.tax_amount or 0),
                "摘要": it.description,
                "发票张数": len(it.invoices),
            }
            for it in r.items
        ],
        "关联发票": invoices,
        "审批历史": [
            {
                "动作": lg.action,
                "操作人": lg.operator,
                "时间": lg.created_at.isoformat(sep=" ", timespec="seconds") if lg.created_at else None,
                "级别": lg.level,
                "意见": lg.comment,
            }
            for lg in r.logs
        ],
    }


def bookkeeping_payload(db: Session, invoices: list[m.Invoice]) -> dict:
    """AI 记账的输入：待编的发票 + 所有可选 id 清单。"""
    return {
        "invoices": [
            {
                "id": v.id,
                "invoice_no": v.invoice_no,
                "invoice_type": v.invoice_type,
                "amount": float(v.amount or 0),
                "tax_amount": float(v.tax_amount or 0),
                "tax_rate": float(v.tax_rate or 0),
                "invoice_date": v.invoice_date.isoformat() if v.invoice_date else None,
                "seller_name": v.seller_name,
                "buyer_name": v.buyer_name,
                "category": v.category.name if v.category else None,
                "remark": v.remark,
            }
            for v in invoices
        ],
        "categories": [
            {"id": c.id, "name": c.name, "group": c.group_name}
            for c in db.query(m.ExpenseCategory).order_by(m.ExpenseCategory.id).all()
        ],
        "departments": [
            {"id": d.id, "name": d.name} for d in db.query(m.Department).order_by(m.Department.id).all()
        ],
        "customers": [
            {"id": c.id, "name": c.name}
            for c in db.query(m.Customer).order_by(m.Customer.id.desc()).limit(MAX_LIST).all()
        ],
        "projects": [
            {"id": p.id, "name": p.name}
            for p in db.query(m.Project).order_by(m.Project.id.desc()).limit(MAX_LIST).all()
        ],
    }


def usage_summary(db: Session, days: int = 30) -> dict:
    """用量统计：按场景与模型汇总，给管理员看成本。"""
    since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = since.fromordinal(since.toordinal() - max(days - 1, 0))
    rows = db.query(m.AiUsage).filter(m.AiUsage.created_at >= since).all()
    total = len(rows)
    failed = sum(1 for r in rows if not r.ok)
    tokens = sum(r.total_tokens or 0 for r in rows)
    by_kind: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    for r in rows:
        for bucket, key in ((by_kind, r.kind), (by_model, r.model or "-")):
            slot = bucket.setdefault(key, {"calls": 0, "failed": 0, "tokens": 0, "latency_ms": 0})
            slot["calls"] += 1
            slot["failed"] += 0 if r.ok else 1
            slot["tokens"] += r.total_tokens or 0
            slot["latency_ms"] += r.latency_ms or 0
    for bucket in (by_kind, by_model):
        for slot in bucket.values():
            slot["avg_latency_ms"] = int(slot["latency_ms"] / slot["calls"]) if slot["calls"] else 0
            slot.pop("latency_ms", None)
    return {
        "days": days,
        "total_calls": total,
        "failed_calls": failed,
        "total_tokens": tokens,
        "by_kind": [{"kind": k, **v} for k, v in sorted(by_kind.items(), key=lambda x: -x[1]["calls"])],
        "by_model": [{"model": k, **v} for k, v in sorted(by_model.items(), key=lambda x: -x[1]["calls"])],
        "recent": [
            {
                "kind": r.kind, "model": r.model, "username": r.username, "ok": r.ok,
                "tokens": r.total_tokens, "latency_ms": r.latency_ms, "error": r.error,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(sep=" ", timespec="seconds") if r.created_at else None,
            }
            for r in sorted(rows, key=lambda x: x.id or 0, reverse=True)[:30]
        ],
    }
