"""扫码核验 API（v2.8.0）

GET /api/scan/resolve?code=xxx

背景：打印单据左下角的 Data Matrix 里只放一个「单号」（码图小、单数据区、易扫），
手机扫出来后由本接口把单号换回具体记录，前端再打开对应详情页。

识别规则（自上而下命中即停）：
  - INV-LIST-*     → 发票清单批次号（无具体记录，前端跳发票台账）
  - 非纯数字       → 当报销单编号查（reimbursement.code，如 BX20260924001）
  - 8~20 位纯数字   → 当发票号码查（invoice.invoice_no）
  - 都没命中        → 两类互换再试一次

权限：报销单套 sec.scope_conds、发票套 sec.invoice_scope_conds。
      查不到统一回 found=false —— 不区分「不存在」与「无权限」，
      避免有人拿扫码接口当探测器枚举别人的单号。
"""
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models as m
from .. import security as sec
from ..database import get_db

router = APIRouter(prefix="/api/scan", tags=["扫码"])

_LIST_PREFIX = "INV-LIST-"
_RE_PURE_DIGITS = re.compile(r"^\d{8,20}$")


def _money(v) -> str:
    try:
        return f"¥{float(v or 0):,.2f}"
    except (TypeError, ValueError):
        return "¥0.00"


def _reimb_out(r: m.Reimbursement) -> dict:
    parts = [
        p
        for p in (
            r.applicant.name if r.applicant else "",
            r.department.name if r.department else "",
            _money(r.total_amount),
            r.status or "",
        )
        if p
    ]
    return {
        "found": True,
        "type": "reimbursement",
        "id": r.id,
        "code": r.code,
        "title": r.title or f"报销单 {r.code}",
        "subtitle": " · ".join(parts),
    }


def _invoice_out(v: m.Invoice) -> dict:
    parts = [
        p
        for p in (
            v.seller_name or "",
            _money(v.amount),
            v.invoice_date.isoformat() if v.invoice_date else "",
        )
        if p
    ]
    return {
        "found": True,
        "type": "invoice",
        "id": v.id,
        "code": v.invoice_no,
        "title": f"发票 {v.invoice_no}",
        "subtitle": " · ".join(parts),
    }


def _find_reimb(db: Session, code: str, user: m.AppUser):
    return (
        db.query(m.Reimbursement)
        .filter(m.Reimbursement.code == code, *sec.scope_conds(user))
        .first()
    )


def _find_invoice(db: Session, no: str, user: m.AppUser):
    return (
        db.query(m.Invoice)
        .filter(m.Invoice.invoice_no == no, *sec.invoice_scope_conds(user))
        .order_by(m.Invoice.id.desc())
        .first()
    )


@router.get("/resolve", response_model=dict)
def resolve(
    code: str = Query(..., min_length=1, max_length=64, description="扫码得到的原始内容"),
    user: Annotated[m.AppUser, Depends(sec.current_user)] = None,
    db: Session = Depends(get_db),
):
    """把扫描到的编码解析成可跳转的记录。"""
    raw = (code or "").strip()
    up = raw.upper()
    pure_digits = bool(_RE_PURE_DIGITS.match(raw))

    # 1) 发票清单批次号：没有对应记录，前端跳发票台账即可
    if up.startswith(_LIST_PREFIX):
        return {
            "found": True,
            "type": "invoice_list",
            "id": None,
            "code": up,
            "title": "发票清单",
            "subtitle": "打印批次 " + up,
        }

    # 2) 非纯数字 → 优先当报销单编号
    if not pure_digits:
        r = _find_reimb(db, up, user)
        if r:
            return _reimb_out(r)

    # 3) 纯 8~20 位数字 → 当发票号码
    if pure_digits:
        v = _find_invoice(db, raw, user)
        if v:
            return _invoice_out(v)

    # 4) 兜底：两类互换再试一次（应对编号格式变化）
    r = _find_reimb(db, up, user)
    if r:
        return _reimb_out(r)
    v = _find_invoice(db, raw, user)
    if v:
        return _invoice_out(v)

    return {
        "found": False,
        "type": None,
        "id": None,
        "code": raw,
        "title": "",
        "subtitle": "",
        "hint": "没有找到该编号对应的记录（也可能它不在你的查看范围内）",
    }
