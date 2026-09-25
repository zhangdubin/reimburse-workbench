"""ORM -> dict 序列化助手。

手工构造 dict 而不是依赖 Pydantic 的 from_attributes，
是为了把关联名称（部门名/申请人/客户名…）一次性带出来，避免前端二次查询。
"""

from __future__ import annotations

from . import models as m


def money(v) -> float:
    return round(float(v or 0), 2)


def date_s(v):
    return v.isoformat() if v else None


def dt_s(v):
    return v.isoformat(sep=" ", timespec="seconds") if v else None


def department_out(o: m.Department) -> dict:
    return {"id": o.id, "name": o.name, "code": o.code, "manager": o.manager, "remark": o.remark}


def employee_out(o: m.Employee) -> dict:
    return {
        "id": o.id,
        "name": o.name,
        "employee_no": o.employee_no,
        "department_id": o.department_id,
        "department_name": o.department.name if o.department else None,
        "position": o.position,
        "level": o.level,
        "email": o.email,
        "phone": o.phone,
        "bank_account": o.bank_account,
        "active": o.active,
    }


def category_out(o: m.ExpenseCategory) -> dict:
    return {
        "id": o.id,
        "name": o.name,
        "code": o.code,
        "group_name": o.group_name,
        "requires_invoice": o.requires_invoice,
        "single_limit": money(o.single_limit),
        "daily_limit": money(o.daily_limit),
        "monthly_limit": money(o.monthly_limit),
        "active": o.active,
        "remark": o.remark,
    }


def customer_out(o: m.Customer) -> dict:
    return {
        "id": o.id,
        "name": o.name,
        "code": o.code,
        "contact": o.contact,
        "phone": o.phone,
        "industry": o.industry,
        "remark": o.remark,
    }


def project_out(o: m.Project) -> dict:
    return {
        "id": o.id,
        "name": o.name,
        "code": o.code,
        "customer_id": o.customer_id,
        "customer_name": o.customer.name if o.customer else None,
        "manager": o.manager,
        "stage": o.stage,
        "status": o.status,
    }


def budget_out(o: m.Budget, used: float = 0.0) -> dict:
    amount = money(o.amount)
    return {
        "id": o.id,
        "year": o.year,
        "month": o.month,
        "department_id": o.department_id,
        "department_name": o.department.name if o.department else None,
        "project_id": o.project_id,
        "project_name": o.project.name if o.project else None,
        "category_id": o.category_id,
        "category_name": o.category.name if o.category else None,
        "amount": amount,
        "remark": o.remark,
        "used": money(used),
        "usage_rate": round(used / amount * 100, 1) if amount else 0.0,
    }


def item_out(o: m.ReimbursementItem) -> dict:
    return {
        "id": o.id,
        "category_id": o.category_id,
        "category_name": o.category.name if o.category else None,
        "occur_date": date_s(o.occur_date),
        "amount": money(o.amount),
        "tax_amount": money(o.tax_amount),
        "description": o.description,
        "remark": o.remark,
        "invoice_count": len(o.invoices) if o.invoices is not None else 0,
    }


def log_out(o: m.ApprovalLog) -> dict:
    return {
        "id": o.id,
        "action": o.action,
        "operator": o.operator,
        "from_status": o.from_status,
        "to_status": o.to_status,
        "level": o.level,
        "comment": o.comment,
        "created_at": dt_s(o.created_at),
    }


def reimbursement_out(o: m.Reimbursement) -> dict:
    approved = o.approved_level or 0
    required = max(o.required_level or 1, 1)
    # 待审级别只在「待审批」状态下有意义；其余情况给 0，前端据此决定是否显示进度
    pending = min(approved + 1, required) if o.status == m.ST_PENDING else 0
    return {
        "id": o.id,
        "code": o.code,
        "title": o.title,
        "applicant_id": o.applicant_id,
        "applicant_name": o.applicant.name if o.applicant else None,
        "department_id": o.department_id,
        "department_name": o.department.name if o.department else None,
        "project_id": o.project_id,
        "project_name": o.project.name if o.project else None,
        "customer_id": o.customer_id,
        "customer_name": o.customer.name if o.customer else None,
        "purpose": o.purpose,
        "status": o.status,
        "total_amount": money(o.total_amount),
        "invoice_count": o.invoice_count,
        "occur_start": date_s(o.occur_start),
        "occur_end": date_s(o.occur_end),
        "submit_at": dt_s(o.submit_at),
        "approve_at": dt_s(o.approve_at),
        "pay_at": dt_s(o.pay_at),
        "approver": o.approver,
        "approver_id": o.approver_id,
        "required_level": required,
        "approved_level": approved,
        "pending_level": pending,
        "reject_reason": o.reject_reason,
        "remark": o.remark,
        "created_at": dt_s(o.created_at),
        "updated_at": dt_s(o.updated_at),
    }


def reimbursement_detail(o: m.Reimbursement) -> dict:
    d = reimbursement_out(o)
    d["items"] = [item_out(i) for i in o.items]
    d["invoices"] = [invoice_out(v) for v in o.invoices]
    d["logs"] = [log_out(lg) for lg in o.logs]
    return d


def invoice_out(o: m.Invoice) -> dict:
    return {
        "id": o.id,
        "invoice_no": o.invoice_no,
        "invoice_code": o.invoice_code,
        "invoice_type": o.invoice_type,
        "amount": money(o.amount),
        "tax_rate": money(o.tax_rate),
        "tax_amount": money(o.tax_amount),
        "invoice_date": date_s(o.invoice_date),
        "seller_name": o.seller_name,
        "seller_tax_no": o.seller_tax_no,
        "buyer_name": o.buyer_name,
        "buyer_tax_no": o.buyer_tax_no,
        "check_status": o.check_status,
        "check_result": o.check_result,
        "category_id": o.category_id,
        "category_name": o.category.name if o.category else None,
        "reimbursement_id": o.reimbursement_id,
        "reimbursement_code": o.reimbursement.code if o.reimbursement else None,
        "item_id": o.item_id,
        "file_name": o.file_name,
        "source": getattr(o, "source", None),
        "created_by": o.created_by,
        "recognize_score": float(o.recognize_score) if getattr(o, "recognize_score", None) is not None else None,
        "recognize_from": getattr(o, "recognize_from", None),
        # 有没有影像文件，前端据此决定是否显示「影像」按钮上的小圆点
        "attachment_count": len(o.attachments) if hasattr(o, "attachments") else 0,
        "remark": o.remark,
        "created_at": dt_s(o.created_at),
    }
