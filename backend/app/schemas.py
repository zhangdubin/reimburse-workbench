"""Pydantic 数据契约。金额统一用 float 对外暴露，避免 Decimal 在 JSON 里变成字符串。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

Money = Annotated[float, BeforeValidator(lambda v: 0.0 if v is None else float(v))]
MoneyOpt = Annotated[float | None, BeforeValidator(lambda v: None if v is None else float(v))]


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- 主数据
class DepartmentIn(BaseModel):
    name: str
    code: str | None = None
    parent_id: int | None = None
    manager: str | None = None
    cost_center: str | None = None
    is_active: bool = True
    description: str | None = None
    remark: str | None = None


class DepartmentOut(ORMBase):
    id: int
    name: str
    code: str | None = None
    parent_id: int | None = None
    parent_name: str | None = None
    manager: str | None = None
    cost_center: str | None = None
    is_active: bool = True
    description: str | None = None
    remark: str | None = None
    employee_count: int = 0


class EmployeeIn(BaseModel):
    name: str
    employee_no: str
    department_id: int | None = None
    position: str | None = None
    level: str | None = None
    gender: str | None = None
    birthday: date | None = None
    hire_date: date | None = None
    resign_date: date | None = None
    id_card: str | None = None
    address: str | None = None
    emergency_contact: str | None = None
    email: str | None = None
    phone: str | None = None
    bank_account: str | None = None
    active: bool = True
    remark: str | None = None


class EmployeeOut(ORMBase):
    id: int
    name: str
    employee_no: str
    department_id: int | None = None
    department_name: str | None = None
    position: str | None = None
    level: str | None = None
    gender: str | None = None
    birthday: date | None = None
    hire_date: date | None = None
    resign_date: date | None = None
    # id_card 始终打码；详细页面里 serializer 再决定是否打全
    id_card_masked: str | None = None
    address: str | None = None
    emergency_contact: str | None = None
    email: str | None = None
    phone: str | None = None
    bank_account: str | None = None
    active: bool = True
    remark: str | None = None


class CategoryIn(BaseModel):
    name: str
    code: str | None = None
    group_name: str | None = None
    requires_invoice: bool = True
    single_limit: Money = 0
    daily_limit: Money = 0
    monthly_limit: Money = 0
    tax_rate: Money = 0
    acc_subject: str | None = None
    active: bool = True
    remark: str | None = None


class CategoryOut(ORMBase):
    id: int
    name: str
    code: str | None = None
    group_name: str | None = None
    requires_invoice: bool = True
    single_limit: Money = 0
    daily_limit: Money = 0
    monthly_limit: Money = 0
    tax_rate: Money = 0
    acc_subject: str | None = None
    active: bool = True
    remark: str | None = None


class CustomerIn(BaseModel):
    name: str
    code: str | None = None
    contact: str | None = None
    phone: str | None = None
    industry: str | None = None
    tax_no: str | None = None
    address: str | None = None
    website: str | None = None
    bank_info: str | None = None
    remark: str | None = None


class CustomerOut(ORMBase):
    id: int
    name: str
    code: str | None = None
    contact: str | None = None
    phone: str | None = None
    industry: str | None = None
    tax_no: str | None = None
    address: str | None = None
    website: str | None = None
    bank_info: str | None = None
    remark: str | None = None


class ProjectIn(BaseModel):
    name: str
    code: str | None = None
    customer_id: int | None = None
    manager: str | None = None
    stage: str | None = None
    status: str = "进行中"
    start_date: date | None = None
    end_date: date | None = None
    remark: str | None = None


class ProjectOut(ORMBase):
    id: int
    name: str
    code: str | None = None
    customer_id: int | None = None
    customer_name: str | None = None
    manager: str | None = None
    stage: str | None = None
    status: str = "进行中"
    start_date: date | None = None       # v2.9.17：项目起止
    end_date: date | None = None
    remark: str | None = None           # v2.9.17：项目说明


class BudgetIn(BaseModel):
    year: int
    month: int | None = None
    department_id: int | None = None
    project_id: int | None = None
    category_id: int | None = None
    amount: Money = 0
    remark: str | None = None


class BudgetOut(ORMBase):
    id: int
    year: int
    month: int | None = None
    department_id: int | None = None
    project_id: int | None = None
    category_id: int | None = None
    amount: Money = 0
    remark: str | None = None
    department_name: str | None = None
    project_name: str | None = None
    category_name: str | None = None
    used: Money = 0
    usage_rate: float = 0.0


# ---------------------------------------------------------------- 报销单
class ItemIn(BaseModel):
    id: int | None = None
    category_id: int | None = None
    occur_date: date | None = None
    amount: Money = 0
    tax_amount: Money = 0
    description: str | None = None
    remark: str | None = None


class ItemOut(ORMBase):
    id: int
    category_id: int | None = None
    category_name: str | None = None
    occur_date: date | None = None
    amount: Money = 0
    tax_amount: Money = 0
    description: str | None = None
    remark: str | None = None
    invoice_count: int = 0


class ReimbursementIn(BaseModel):
    title: str
    applicant_id: int | None = None
    department_id: int | None = None
    project_id: int | None = None
    customer_id: int | None = None
    purpose: str | None = None
    occur_start: date | None = None
    occur_end: date | None = None
    remark: str | None = None
    items: list[ItemIn] = Field(default_factory=list)


class ReimbursementOut(ORMBase):
    id: int
    code: str
    title: str
    applicant_id: int | None = None
    applicant_name: str | None = None
    department_id: int | None = None
    department_name: str | None = None
    project_id: int | None = None
    project_name: str | None = None
    customer_id: int | None = None
    customer_name: str | None = None
    purpose: str | None = None
    status: str
    total_amount: Money = 0
    invoice_count: int = 0
    occur_start: date | None = None
    occur_end: date | None = None
    submit_at: datetime | None = None
    approve_at: datetime | None = None
    pay_at: datetime | None = None
    approver: str | None = None
    approver_id: int | None = None
    required_level: int = 1
    approved_level: int = 0
    pending_level: int = 0
    reject_reason: str | None = None
    remark: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ReimbursementDetail(ReimbursementOut):
    items: list[ItemOut] = Field(default_factory=list)
    invoices: list["InvoiceOut"] = Field(default_factory=list)
    logs: list["ApprovalLogOut"] = Field(default_factory=list)


class Page(BaseModel):
    total: int
    page: int
    page_size: int
    total_amount: MoneyOpt = None  # 列表场景下可选返回「符合条件金额合计」
    items: list[Any]


class ActionIn(BaseModel):
    operator: str | None = None
    comment: str | None = None
    approver: str | None = None


# ---------------------------------------------------------------- 发票
class InvoiceIn(BaseModel):
    invoice_no: str
    invoice_code: str | None = None
    invoice_type: str = "增值税电子普通发票"
    amount: Money = 0
    tax_rate: Money = 0
    tax_amount: Money = 0
    invoice_date: date | None = None
    seller_name: str | None = None
    seller_tax_no: str | None = None
    buyer_name: str | None = None
    buyer_tax_no: str | None = None
    check_status: str = "未查验"
    category_id: int | None = None
    reimbursement_id: int | None = None
    item_id: int | None = None
    file_name: str | None = None
    remark: str | None = None


class InvoiceOut(ORMBase):
    id: int
    invoice_no: str
    invoice_code: str | None = None
    invoice_type: str = "增值税电子普通发票"
    amount: Money = 0
    tax_rate: Money = 0
    tax_amount: Money = 0
    invoice_date: date | None = None
    seller_name: str | None = None
    seller_tax_no: str | None = None
    buyer_name: str | None = None
    buyer_tax_no: str | None = None
    check_status: str = "未查验"
    check_result: str | None = None
    category_id: int | None = None
    category_name: str | None = None
    reimbursement_id: int | None = None
    reimbursement_code: str | None = None
    item_id: int | None = None
    file_name: str | None = None
    # 来源与识别结果：response_model 会把未声明的字段丢掉，这几个必须显式声明，
    # 否则列表里有、单张详情里没有，前端得写两套逻辑。
    source: str | None = None
    created_by: str | None = None
    recognize_score: float | None = None
    recognize_from: str | None = None
    attachment_count: int = 0
    remark: str | None = None
    created_at: datetime | None = None


class ApprovalLogOut(ORMBase):
    id: int
    action: str
    operator: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    level: int | None = None
    comment: str | None = None
    created_at: datetime | None = None


ReimbursementDetail.model_rebuild()
