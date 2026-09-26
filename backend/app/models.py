"""ORM 模型：销售费用报销工作台数据层。"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

MONEY = Numeric(14, 2)

# ---- 业务状态常量 ----
ST_DRAFT = "草稿"
ST_PENDING = "待审批"
ST_APPROVED = "已通过"
ST_REJECTED = "已驳回"
ST_PAID = "已付款"
STATUS_FLOW = [ST_DRAFT, ST_PENDING, ST_APPROVED, ST_PAID]

CHECK_UNVERIFIED = "未查验"
CHECK_OK = "已查验"
CHECK_BAD = "异常"

# ---- 发票来源 ----
SOURCE_MANUAL = "手工登记"
SOURCE_MAIL = "邮箱收票"
SOURCE_IMPORT = "批量导入"
SOURCES = [SOURCE_MANUAL, SOURCE_MAIL, SOURCE_IMPORT]

# ---- 收票结果 ----
IMPORT_OK = "成功"
IMPORT_PARTIAL = "部分成功"
IMPORT_FAILED = "失败"
IMPORT_SKIPPED = "跳过"

# ---- 角色（auth 用）----
ROLE_APPLICANT = "申请人"
ROLE_APPROVER = "审批人"
ROLE_FINANCE = "财务"
ROLE_ADMIN = "管理员"
ROLES = [ROLE_APPLICANT, ROLE_APPROVER, ROLE_FINANCE, ROLE_ADMIN]

# ---- 分级审批 ----
# 报销单的 status 始终保持 4 态不变，"当前卡在第几级"由 required_level / approved_level 表达，
# 这样可以复用既有的状态机、看板口径与前端筛选，不必新增状态值。
LEVEL_NONE = 0
LEVEL_ONE = 1
LEVEL_TWO = 2


class Department(Base):
    """部门（v2.9.17 起支持父子层级、成本中心、启用状态）"""

    __tablename__ = "department"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    code: Mapped[str | None] = mapped_column(String(32), unique=True, default=None)
    # v2.9.17：父子层级（顶级部门 parent_id=NULL；递归 delete 时先挪走 children 防止破坏外键）
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("department.id"), default=None)
    # 部门负责人（v2.9.17 字段保留并加注释：原为姓名字符串，后续想升级为 employee_id 走迁移）
    manager: Mapped[str | None] = mapped_column(String(32), default=None)
    # 成本中心编码：财务侧做预算/费用归集时常需要
    cost_center: Mapped[str | None] = mapped_column(String(32), default=None)
    # 启用状态：软删除，删除部门前的互斥校验需要
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # forward reference 用字符串避免 Employee 还没定义的解析问题
    employees: Mapped[list["Employee"]] = relationship(
        "Employee", back_populates="department", foreign_keys="Employee.department_id",
    )
    # 自引用父子层级：v2.9.17 起仅 viewonly 单边，避免 SA 在自引用上反复推方向
    # 写 parent_id 走 service 层（路由侧 update），不通过 ORM
    children: Mapped[list["Department"]] = relationship(
        "Department",
        primaryjoin="Department.parent_id == Department.id",
        foreign_keys="Department.parent_id",
        viewonly=True,
    )
    parent: Mapped["Department | None"] = relationship(
        "Department",
        primaryjoin="Department.id == foreign(Department.parent_id)",
        foreign_keys="Department.parent_id",
        viewonly=True,
    )


class Employee(Base):
    """员工（报销申请人 / 审批人，v2.9.17 起加详细人事字段）"""

    __tablename__ = "employee"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), index=True)
    employee_no: Mapped[str] = mapped_column(String(32), unique=True)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("department.id"), default=None)
    position: Mapped[str | None] = mapped_column(String(64), default=None)
    level: Mapped[str | None] = mapped_column(String(16), default=None)
    # v2.9.17：人事扩展字段
    gender: Mapped[str | None] = mapped_column(String(8), default=None)        # 男/女/未知
    birthday: Mapped[date | None] = mapped_column(Date, default=None)
    hire_date: Mapped[date | None] = mapped_column(Date, default=None)
    resign_date: Mapped[date | None] = mapped_column(Date, default=None)
    id_card: Mapped[str | None] = mapped_column(String(32), default=None)     # 存明文但 list/serializer 永远打码
    address: Mapped[str | None] = mapped_column(String(255), default=None)
    emergency_contact: Mapped[str | None] = mapped_column(String(64), default=None)  # 「姓名 / 电话」一栏写完
    email: Mapped[str | None] = mapped_column(String(128), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    bank_account: Mapped[str | None] = mapped_column(String(64), default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    department: Mapped["Department | None"] = relationship(back_populates="employees", foreign_keys=[department_id])


class ExpenseCategory(Base):
    """费用类型（v2.9.17 起加税率、会计科目）"""

    __tablename__ = "expense_category"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    code: Mapped[str | None] = mapped_column(String(32), unique=True, default=None)
    group_name: Mapped[str | None] = mapped_column(String(32), default=None)
    requires_invoice: Mapped[bool] = mapped_column(Boolean, default=True)
    single_limit: Mapped[float] = mapped_column(MONEY, default=0)  # 单笔限额，0=不限
    daily_limit: Mapped[float] = mapped_column(MONEY, default=0)  # 单人单日限额
    monthly_limit: Mapped[float] = mapped_column(MONEY, default=0)  # 单人月度限额
    # v2.9.17：进项税抵扣场景要算税价分离；财务对账要按科目走
    tax_rate: Mapped[float] = mapped_column(MONEY, default=0)      # 默认 0=免税/不计税
    acc_subject: Mapped[str | None] = mapped_column(String(64), default=None)  # 会计科目编码
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Customer(Base):
    """客户（v2.9.17 起加税号、地址、官网、银行账号）"""

    __tablename__ = "customer"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    code: Mapped[str | None] = mapped_column(String(32), unique=True, default=None)
    contact: Mapped[str | None] = mapped_column(String(32), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    industry: Mapped[str | None] = mapped_column(String(64), default=None)
    # v2.9.17：客户基础信息
    tax_no: Mapped[str | None] = mapped_column(String(32), default=None)       # 税号（增值税开票用）
    address: Mapped[str | None] = mapped_column(String(255), default=None)
    website: Mapped[str | None] = mapped_column(String(128), default=None)
    bank_info: Mapped[str | None] = mapped_column(String(128), default=None)  # 收款银行 / 账号
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Project(Base):
    """项目 / 商机"""

    __tablename__ = "project"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    code: Mapped[str | None] = mapped_column(String(32), unique=True, default=None)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"), default=None)
    manager: Mapped[str | None] = mapped_column(String(32), default=None)
    stage: Mapped[str | None] = mapped_column(String(32), default=None)
    status: Mapped[str] = mapped_column(String(16), default="进行中")
    # v2.9.17：项目起止 + 备注
    start_date: Mapped[date | None] = mapped_column(Date, default=None)
    end_date: Mapped[date | None] = mapped_column(Date, default=None)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped["Customer | None"] = relationship()


class Budget(Base):
    """费用预算：按 年(月) x 部门/项目/费用类型 组合，空值表示不限定该维度"""

    __tablename__ = "budget"
    __table_args__ = (
        UniqueConstraint(
            "year", "month", "department_id", "project_id", "category_id", name="uq_budget_dim"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int | None] = mapped_column(Integer, default=None)  # None = 全年预算
    department_id: Mapped[int | None] = mapped_column(ForeignKey("department.id"), default=None)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"), default=None)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("expense_category.id"), default=None)
    amount: Mapped[float] = mapped_column(MONEY, default=0)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    department: Mapped["Department | None"] = relationship()
    project: Mapped["Project | None"] = relationship()
    category: Mapped["ExpenseCategory | None"] = relationship()


class Reimbursement(Base):
    """报销单"""

    __tablename__ = "reimbursement"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # 单号 BX2026090001
    title: Mapped[str] = mapped_column(String(128))
    applicant_id: Mapped[int | None] = mapped_column(ForeignKey("employee.id"), default=None)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("department.id"), default=None)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"), default=None)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"), default=None)
    purpose: Mapped[str | None] = mapped_column(Text, default=None)  # 事由
    status: Mapped[str] = mapped_column(String(16), default=ST_DRAFT, index=True)
    total_amount: Mapped[float] = mapped_column(MONEY, default=0)
    invoice_count: Mapped[int] = mapped_column(Integer, default=0)
    occur_start: Mapped[date | None] = mapped_column(Date, default=None)
    occur_end: Mapped[date | None] = mapped_column(Date, default=None)
    submit_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    approve_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    pay_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    approver: Mapped[str | None] = mapped_column(String(32), default=None)
    approver_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id"), default=None, index=True
    )
    # 分级审批：required_level 由金额与阈值算出；approved_level 记录已通过的级数
    required_level: Mapped[int] = mapped_column(Integer, default=LEVEL_ONE)
    approved_level: Mapped[int] = mapped_column(Integer, default=LEVEL_NONE)
    reject_reason: Mapped[str | None] = mapped_column(Text, default=None)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    applicant: Mapped["Employee | None"] = relationship(foreign_keys=[applicant_id])
    department: Mapped["Department | None"] = relationship()
    project: Mapped["Project | None"] = relationship()
    customer: Mapped["Customer | None"] = relationship()
    items: Mapped[list["ReimbursementItem"]] = relationship(
        back_populates="reimbursement", cascade="all, delete-orphan", order_by="ReimbursementItem.id"
    )
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="reimbursement")
    logs: Mapped[list["ApprovalLog"]] = relationship(
        back_populates="reimbursement",
        cascade="all, delete-orphan",
        order_by="ApprovalLog.id",
    )


class ReimbursementItem(Base):
    """报销单费用明细行"""

    __tablename__ = "reimbursement_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    reimbursement_id: Mapped[int] = mapped_column(
        ForeignKey("reimbursement.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("expense_category.id"), default=None)
    occur_date: Mapped[date | None] = mapped_column(Date, default=None)
    amount: Mapped[float] = mapped_column(MONEY, default=0)
    tax_amount: Mapped[float] = mapped_column(MONEY, default=0)
    description: Mapped[str | None] = mapped_column(String(255), default=None)
    remark: Mapped[str | None] = mapped_column(Text, default=None)

    reimbursement: Mapped["Reimbursement"] = relationship(back_populates="items")
    category: Mapped["ExpenseCategory | None"] = relationship()
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="item")


class Invoice(Base):
    """发票台账"""

    __tablename__ = "invoice"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String(32), index=True)  # 发票号码
    invoice_code: Mapped[str | None] = mapped_column(String(32), default=None)  # 发票代码
    invoice_type: Mapped[str] = mapped_column(String(32), default="增值税电子普通发票")
    amount: Mapped[float] = mapped_column(MONEY, default=0)  # 价税合计
    tax_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    tax_amount: Mapped[float] = mapped_column(MONEY, default=0)
    invoice_date: Mapped[date | None] = mapped_column(Date, default=None)
    seller_name: Mapped[str | None] = mapped_column(String(128), default=None)
    seller_tax_no: Mapped[str | None] = mapped_column(String(32), default=None)
    buyer_name: Mapped[str | None] = mapped_column(String(128), default=None)
    buyer_tax_no: Mapped[str | None] = mapped_column(String(32), default=None)
    check_status: Mapped[str] = mapped_column(String(16), default=CHECK_UNVERIFIED, index=True)
    check_result: Mapped[str | None] = mapped_column(String(255), default=None)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("expense_category.id"), default=None)
    reimbursement_id: Mapped[int | None] = mapped_column(
        ForeignKey("reimbursement.id"), default=None, index=True
    )
    item_id: Mapped[int | None] = mapped_column(ForeignKey("reimbursement_item.id"), default=None)
    file_name: Mapped[str | None] = mapped_column(String(255), default=None)
    # 登记人（用户名）。散票（未关联报销单）的可见性靠它来界定
    created_by: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    # 来源：手工登记 / 邮箱收票 / 批量导入，用于追责与统计
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_MANUAL, index=True)
    # 识别置信度 0~1，邮箱收票自动入账时用它标出「需要人工复核」的票
    recognize_score: Mapped[float | None] = mapped_column(Numeric(4, 2), default=None)
    recognize_from: Mapped[str | None] = mapped_column(String(16), default=None)  # ofd/xml/pdf/text…
    mail_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("mail_message.id"), default=None, index=True
    )
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    reimbursement: Mapped["Reimbursement | None"] = relationship(back_populates="invoices")
    item: Mapped["ReimbursementItem | None"] = relationship(back_populates="invoices")
    category: Mapped["ExpenseCategory | None"] = relationship()
    attachments: Mapped[list["InvoiceAttachment"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class ApprovalLog(Base):
    """报销单流转日志"""

    __tablename__ = "approval_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    reimbursement_id: Mapped[int] = mapped_column(
        ForeignKey("reimbursement.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(16))  # 创建/提交/通过/驳回/付款
    operator: Mapped[str | None] = mapped_column(String(32), default=None)
    from_status: Mapped[str | None] = mapped_column(String(16), default=None)
    to_status: Mapped[str | None] = mapped_column(String(16), default=None)
    level: Mapped[int | None] = mapped_column(Integer, default=None)  # 本次操作对应的审批级
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    reimbursement: Mapped["Reimbursement"] = relationship(back_populates="logs")


class AppUser(Base):
    """后台账号。表名刻意用 app_user：user 在 PostgreSQL 里是保留字。"""

    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # pbkdf2_sha256$iter$salt$hash
    name: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_APPLICANT, index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employee.id"), default=None)
    # 0=无审批权，1=一级审批人，2=二级审批人；仅 role=审批人 时有意义
    approval_level: Mapped[int] = mapped_column(Integer, default=LEVEL_NONE)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    employee: Mapped["Employee | None"] = relationship()

    @property
    def department_id(self) -> int | None:
        return self.employee.department_id if self.employee else None


class UserSession(Base):
    """登录会话。token 只存哈希，即使库被读走也无法直接冒用。"""

    __tablename__ = "user_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), index=True)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["AppUser"] = relationship()


class AuditLog(Base):
    """操作审计：所有写操作与登录事件，记录到人。"""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), default=None, index=True)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    role: Mapped[str | None] = mapped_column(String(16), default=None)
    action: Mapped[str] = mapped_column(String(32), index=True)  # 登录/登出/创建/更新/删除/审批/付款…
    entity: Mapped[str | None] = mapped_column(String(32), default=None)  # reimbursement/invoice/user…
    entity_id: Mapped[str | None] = mapped_column(String(64), default=None)
    method: Mapped[str | None] = mapped_column(String(8), default=None)
    path: Mapped[str | None] = mapped_column(String(255), default=None)
    status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)  # 变更摘要 / 失败原因
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class InvoiceAttachment(Base):
    """发票影像（PDF/图片）元数据，实体文件落在 UPLOAD_DIR。"""

    __tablename__ = "invoice_attachment"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoice.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))  # 原始文件名，仅用于展示与下载命名
    stored_name: Mapped[str] = mapped_column(String(255), unique=True)  # 磁盘上的实际文件名
    size: Mapped[int] = mapped_column(Integer, default=0)
    mime: Mapped[str | None] = mapped_column(String(128), default=None)
    uploaded_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    invoice: Mapped["Invoice"] = relationship(back_populates="attachments")


class Setting(Base):
    """系统参数（分级审批阈值等），key-value 便于后台直接改。"""

    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, default=None)
    remark: Mapped[str | None] = mapped_column(String(255), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MailAccount(Base):
    """收票邮箱账号（IMAP）。

    密码必须可逆回读（IMAP 登录要用明文），所以存的是 crypt.py 加密后的密文，
    密钥来自环境变量 MAIL_SECRET，数据库单独泄露也解不开。
    """

    __tablename__ = "mail_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)  # 展示名，如「财务部收票箱」
    host: Mapped[str] = mapped_column(String(128))
    port: Mapped[int] = mapped_column(Integer, default=993)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    username: Mapped[str] = mapped_column(String(128))
    password_enc: Mapped[str] = mapped_column(String(512), default="")
    folder: Mapped[str] = mapped_column(String(64), default="INBOX")
    # 收票口径
    only_unseen: Mapped[bool] = mapped_column(Boolean, default=True)  # 只收未读
    mark_seen: Mapped[bool] = mapped_column(Boolean, default=True)  # 收完标已读，避免重复收
    since_days: Mapped[int] = mapped_column(Integer, default=30)  # 只看最近 N 天
    max_per_sync: Mapped[int] = mapped_column(Integer, default=30)  # 单次最多收几封
    subject_keywords: Mapped[str | None] = mapped_column(Text, default=None)  # 逗号分隔，空=不过滤
    sender_allow: Mapped[str | None] = mapped_column(Text, default=None)  # 发件人白名单，支持 *
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 同步状态
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_sync_status: Mapped[str | None] = mapped_column(String(16), default=None)
    last_sync_detail: Mapped[str | None] = mapped_column(Text, default=None)
    imported_total: Mapped[int] = mapped_column(Integer, default=0)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MailMessage(Base):
    """收件记录：一封邮件一行，同时承担「按 Message-ID 去重」的职责。"""

    __tablename__ = "mail_message"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("mail_account.id"), default=None, index=True)
    uid: Mapped[str | None] = mapped_column(String(32), default=None)
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    subject: Mapped[str | None] = mapped_column(String(255), default=None)
    sender: Mapped[str | None] = mapped_column(String(255), default=None)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=IMPORT_OK, index=True)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

    account: Mapped["MailAccount | None"] = relationship()


# ---------------------------------------------------------------- AI 智能

# 一次 AI 调用的用途。用于用量统计与审计筛选。
AI_CHAT = "助手问答"
AI_RECOGNIZE = "发票识别"
AI_CLASSIFY = "费用分类"
AI_ANALYZE = "单据分析"
AI_APPROVAL = "审批建议"
AI_BOOKKEEPING = "AI 记账"
AI_KINDS = [AI_CHAT, AI_RECOGNIZE, AI_CLASSIFY, AI_ANALYZE, AI_APPROVAL, AI_BOOKKEEPING]


class AiProvider(Base):
    """大模型接入配置（一个 provider 一个模型端点）。

    API Key 与邮箱密码同样的处理：`crypt.py` 可逆加密后落库，接口只回显掩码。
    支持一个默认 provider + 多个备用，界面上可以随时切换而不必改代码。
    """

    __tablename__ = "ai_provider"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)  # 展示名，如「DeepSeek 主账号」
    preset: Mapped[str] = mapped_column(String(32), default="custom")  # 预设厂商 key
    base_url: Mapped[str] = mapped_column(String(255), default="")
    api_key_enc: Mapped[str] = mapped_column(String(1024), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    # 视觉模型：留空表示这家不支持读图，图片类发票会退回 OCR / 人工补录
    vision_model: Mapped[str | None] = mapped_column(String(128), default=None)
    auth_style: Mapped[str] = mapped_column(String(16), default="bearer")  # bearer / api-key / none
    extra_headers: Mapped[str | None] = mapped_column(Text, default=None)  # JSON
    extra_query: Mapped[str | None] = mapped_column(Text, default=None)  # JSON，Azure 的 api-version
    temperature: Mapped[float] = mapped_column(Numeric(3, 2), default=0.3)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=60)
    # 功能开关：由管理员决定这家参与哪些场景，避免把小模型用在复杂分析上
    use_assistant: Mapped[bool] = mapped_column(Boolean, default=True)
    use_recognize: Mapped[bool] = mapped_column(Boolean, default=True)
    use_analyze: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 最近一次连通性测试结果，界面上直接显示，省得每次都要点一次测试
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    last_test_detail: Mapped[str | None] = mapped_column(Text, default=None)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AiUsage(Base):
    """AI 调用流水。带 prompt/回复长度与耗时，用于成本核算与问题排查。"""

    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), default=None, index=True)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    kind: Mapped[str] = mapped_column(String(16), default=AI_CHAT, index=True)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_provider.id"), default=None)
    provider_name: Mapped[str | None] = mapped_column(String(64), default=None)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

