"""系统参数与分级审批规则。

参数存 setting 表（key-value），好处是运维在后台就能改，不必改代码重新发版。
读取统一走 `get_float` / `get_int`，带默认值兜底，缺行或脏数据都不会让接口 500。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from . import models as m

# key -> (默认值, 说明)
DEFAULT_SETTINGS: dict[str, tuple[str, str]] = {
    "approval_level2_threshold": (
        "20000",
        "超过该金额需要二级审批（元）",
    ),
    "approval_overdue_days": ("7", "待审批超期预警天数"),
    "large_amount_threshold": ("20000", "大额报销单预警阈值（元）"),
    "auto_approve_level1": (
        "0",
        "一级审批是否自动通过（1=提交后直接进入二级，用于部门经理缺位时）",
    ),
    "ai_recognize_on_ingest": (
        "0",
        "邮箱/自动收票时是否再用大模型补全字段（1=开启）。开启后每张票都要调一次模型，"
        "会明显增加耗时与调用费用；手工上传识别不受此开关影响，始终可用。",
    ),
}


def ensure_default_settings(db: Session) -> int:
    """补齐缺失的默认参数，返回新增条数。已有值一律不动。"""
    existing = {k for (k,) in db.query(m.Setting.key).all()}
    added = 0
    for key, (value, remark) in DEFAULT_SETTINGS.items():
        if key not in existing:
            db.add(m.Setting(key=key, value=value, remark=remark))
            added += 1
    if added:
        db.commit()
    return added


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(m.Setting, key)
    if row is None or row.value is None:
        return DEFAULT_SETTINGS.get(key, (default, ""))[0]
    return row.value


def get_float(db: Session, key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(db, key, str(default)))
    except (TypeError, ValueError):
        return default


def get_int(db: Session, key: str, default: int = 0) -> int:
    try:
        return int(float(get_setting(db, key, str(default))))
    except (TypeError, ValueError):
        return default


def set_setting(db: Session, key: str, value: str, remark: str | None = None) -> m.Setting:
    row = db.get(m.Setting, key)
    if row is None:
        row = m.Setting(key=key, value=value, remark=remark or DEFAULT_SETTINGS.get(key, ("", ""))[1])
        db.add(row)
    else:
        row.value = value
        if remark:
            row.remark = remark
    db.commit()
    return row


# ------------------------------------------------------------------ 分级审批
def required_level(db: Session, amount: float) -> int:
    """按金额算出需要几级审批。0 元或不设阈值时按一级处理。"""
    threshold = get_float(db, "approval_level2_threshold", 20000.0)
    if threshold > 0 and float(amount or 0) > threshold:
        return m.LEVEL_TWO
    return m.LEVEL_ONE


def pending_level(r: m.Reimbursement) -> int:
    """当前等待的审批级别（1 或 2）。已通过/已付款的单子返回 0。"""
    if r.status not in (m.ST_PENDING, m.ST_DRAFT):
        return m.LEVEL_NONE
    return min(r.approved_level + 1, max(r.required_level, 1))


def approval_summary(db: Session) -> dict:
    return {
        "level2_threshold": get_float(db, "approval_level2_threshold", 20000.0),
        "overdue_days": get_int(db, "approval_overdue_days", 7),
        "large_amount_threshold": get_float(db, "large_amount_threshold", 20000.0),
    }
