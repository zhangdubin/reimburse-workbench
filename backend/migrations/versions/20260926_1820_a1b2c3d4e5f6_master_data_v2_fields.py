"""主数据扩展：v2.9.17 基础数据字段补全

为费用管理主数据加更丰富的字段，便于对接财务/HR/CRM 常见需求：

Department:
  parent_id        部门层级（NULL=顶级，自引用 FK）
  cost_center      成本中心编码（财务预算/费用归集常用）
  is_active        启用状态（软删除标志）
  description      部门描述
  updated_at       通用审计

Employee:
  gender           性别
  birthday         生日
  hire_date        入职日期
  resign_date      离职日期（NULL=在职）
  id_card          身份证号（明细接口返回打码后值；详情 admin 解码单独处理）
  address          通讯地址
  emergency_contact 紧急联系人（含电话）
  updated_at       通用审计

ExpenseCategory:
  tax_rate         默认税率（用于税价分离记账）
  acc_subject      会计科目编码（财务对账）

Customer:
  tax_no           税号（增值税开票用）
  address          通讯地址
  website          官网
  bank_info        收款银行/账号
  updated_at       通用审计

Project:
  start_date / end_date  项目起止
  remark                项目说明
  updated_at             通用审计

不进 app.models：表结构字面量写死。
可重复执行：所有 op.add_column 都用 checkfirst，重复迁移是空操作。

Revision ID: a1b2c3d4e5f6
Revises: b7d4e1c9a2f3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "b7d4e1c9a2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # ---------- Department ----------
    dept_cols = _cols(bind, "department")
    with op.batch_alter_table("department") as b:
        if "parent_id" not in dept_cols:
            # SQLite batch 模式要求外键有名——给个明确名字避开 alembic ValueError
            b.add_column(sa.Column("parent_id", sa.Integer(),
                                  sa.ForeignKey("department.id", name="fk_department_parent"),
                                  nullable=True))
        if "cost_center" not in dept_cols:
            b.add_column(sa.Column("cost_center", sa.String(32), nullable=True))
        if "is_active" not in dept_cols:
            # 默认值由 SQL 层 server_default 兜底；无需 UPDATE 兼容历史数据
            b.add_column(sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
        if "description" not in dept_cols:
            b.add_column(sa.Column("description", sa.Text(), nullable=True))
        if "updated_at" not in dept_cols:
            b.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True,
                                  server_default=sa.func.now()))

    # ---------- Employee ----------
    emp_cols = _cols(bind, "employee")
    with op.batch_alter_table("employee") as b:
        if "gender" not in emp_cols:
            b.add_column(sa.Column("gender", sa.String(8), nullable=True))
        if "birthday" not in emp_cols:
            b.add_column(sa.Column("birthday", sa.Date(), nullable=True))
        if "hire_date" not in emp_cols:
            b.add_column(sa.Column("hire_date", sa.Date(), nullable=True))
        if "resign_date" not in emp_cols:
            b.add_column(sa.Column("resign_date", sa.Date(), nullable=True))
        if "id_card" not in emp_cols:
            b.add_column(sa.Column("id_card", sa.String(32), nullable=True))
        if "address" not in emp_cols:
            b.add_column(sa.Column("address", sa.String(255), nullable=True))
        if "emergency_contact" not in emp_cols:
            b.add_column(sa.Column("emergency_contact", sa.String(64), nullable=True))
        if "updated_at" not in emp_cols:
            b.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True,
                                  server_default=sa.func.now()))

    # ---------- ExpenseCategory ----------
    cat_cols = _cols(bind, "expense_category")
    with op.batch_alter_table("expense_category") as b:
        if "tax_rate" not in cat_cols:
            b.add_column(sa.Column("tax_rate", sa.Numeric(18, 4), nullable=False, server_default="0"))
        if "acc_subject" not in cat_cols:
            b.add_column(sa.Column("acc_subject", sa.String(64), nullable=True))
        if "updated_at" not in cat_cols:
            b.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True,
                                  server_default=sa.func.now()))

    # ---------- Customer ----------
    cus_cols = _cols(bind, "customer")
    with op.batch_alter_table("customer") as b:
        if "tax_no" not in cus_cols:
            b.add_column(sa.Column("tax_no", sa.String(32), nullable=True))
        if "address" not in cus_cols:
            b.add_column(sa.Column("address", sa.String(255), nullable=True))
        if "website" not in cus_cols:
            b.add_column(sa.Column("website", sa.String(128), nullable=True))
        if "bank_info" not in cus_cols:
            b.add_column(sa.Column("bank_info", sa.String(128), nullable=True))
        if "updated_at" not in cus_cols:
            b.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True,
                                  server_default=sa.func.now()))

    # ---------- Project ----------
    prj_cols = _cols(bind, "project")
    with op.batch_alter_table("project") as b:
        if "start_date" not in prj_cols:
            b.add_column(sa.Column("start_date", sa.Date(), nullable=True))
        if "end_date" not in prj_cols:
            b.add_column(sa.Column("end_date", sa.Date(), nullable=True))
        if "remark" not in prj_cols:
            b.add_column(sa.Column("remark", sa.Text(), nullable=True))
        if "updated_at" not in prj_cols:
            b.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True,
                                  server_default=sa.func.now()))


def _cols(bind, table: str) -> set[str]:
    """读出指定表已有的列名，跨 SQLite / PostgreSQL。"""
    if bind.dialect.name == "sqlite":
        rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return {r[1] for r in rows}
    rows = bind.execute(sa.text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
    ), {"t": table}).fetchall()
    return {r[0] for r in rows}


def downgrade() -> None:
    # 倒着删；外键列（parent_id）最后处理
    bind = op.get_bind()
    for tbl, cols in [
        ("project", ["updated_at", "remark", "end_date", "start_date"]),
        ("customer", ["updated_at", "bank_info", "website", "address", "tax_no"]),
        ("expense_category", ["updated_at", "acc_subject", "tax_rate"]),
        ("employee", ["updated_at", "emergency_contact", "address", "id_card",
                       "resign_date", "hire_date", "birthday", "gender"]),
        ("department", ["updated_at", "description", "is_active", "cost_center", "parent_id"]),
    ]:
        existing = _cols(bind, tbl)
        with op.batch_alter_table(tbl) as b:
            for c in cols:
                if c in existing:
                    b.drop_column(c)