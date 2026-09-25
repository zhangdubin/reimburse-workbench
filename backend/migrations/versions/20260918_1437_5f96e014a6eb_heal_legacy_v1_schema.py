"""heal legacy v1 schema（把 v1 老库补齐到 v2 结构）

背景
----
v1 用 `Base.metadata.create_all` 建库，没有 alembic_version 表。
启动时 `stamp_legacy_if_needed()` 会把它标记成初始版本 cc5660459504 再往上走。
但初始迁移描述的是「v2 全量 schema」——对 v1 库来说整条被跳过，
于是 v2 新增的表与字段在库里根本不存在，服务一起来就会因为查不到 app_user 而崩。

这条迁移专门补这一段差额，并且**完全幂等**：
- 表用 checkfirst 建，已存在就跳过
- 字段先反射表结构确认缺失再 add_column
所以从零建的新库跑它等于空操作，不需要分叉。

Revision ID: 5f96e014a6eb
Revises: 4ca09e1c7679
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "5f96e014a6eb"
down_revision = "4ca09e1c7679"
branch_labels = None
depends_on = None


# 这些表 v1 库里本来就有，这里只放一个「桩」让外键能解析到目标列。
# 它们不会被 create —— 出口处会把它们过滤掉。
_STUB_TABLES = {"employee", "invoice"}


def _legacy_tables() -> list[sa.Table]:
    """v1 库里缺失的表，按依赖顺序返回。

    定义与 app/models.py 一致，此处刻意复制而不是 import 模型：
    迁移记录的是「当时发生了什么」，不应随模型演进而改变行为。
    """
    metadata = sa.MetaData()

    # 桩表：仅用于外键解析
    sa.Table("employee", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("invoice", metadata, sa.Column("id", sa.Integer(), primary_key=True))

    app_user = sa.Table(
        "app_user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("role", sa.String(16), nullable=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employee.id"), nullable=True),
        sa.Column("approval_level", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    sa.Index("ix_app_user_username", app_user.c.username, unique=True)
    sa.Index("ix_app_user_role", app_user.c.role)

    user_session = sa.Table(
        "user_session",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    sa.Index("ix_user_session_token_hash", user_session.c.token_hash, unique=True)
    sa.Index("ix_user_session_user_id", user_session.c.user_id)

    audit_log = sa.Table(
        "audit_log",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("role", sa.String(16), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("entity", sa.String(32), nullable=True),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("method", sa.String(8), nullable=True),
        sa.Column("path", sa.String(255), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    sa.Index("ix_audit_log_user_id", audit_log.c.user_id)
    sa.Index("ix_audit_log_action", audit_log.c.action)
    sa.Index("ix_audit_log_created_at", audit_log.c.created_at)

    invoice_attachment = sa.Table(
        "invoice_attachment",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "invoice_id",
            sa.Integer(),
            sa.ForeignKey("invoice.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("stored_name", sa.String(255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("mime", sa.String(128), nullable=True),
        sa.Column("uploaded_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    sa.Index("ix_invoice_attachment_invoice_id", invoice_attachment.c.invoice_id)
    sa.Index("ix_invoice_attachment_stored_name", invoice_attachment.c.stored_name, unique=True)

    sa.Table(
        "setting",
        metadata,
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("remark", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    # 由元数据算依赖顺序：employee → app_user → user_session / audit_log，
    # invoice → invoice_attachment。桩表只是排序用的锚点，不能建。
    return [t for t in metadata.sorted_tables if t.name not in _STUB_TABLES]


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def _has_index(bind, table: str, index: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return index in {i["name"] for i in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # 1) 缺表补齐（新库全部已存在，checkfirst 直接跳过）
    for table in _legacy_tables():
        table.create(bind, checkfirst=True)

    # 2) 缺字段补齐。v1 已有数据，所以非空列一律给 server_default，
    #    否则 Postgres 会拒绝往有数据的表上加 NOT NULL 列。
    adds: list[tuple[str, sa.Column]] = [
        ("reimbursement", sa.Column("approver_id", sa.Integer(), nullable=True)),
        (
            "reimbursement",
            sa.Column("required_level", sa.Integer(), nullable=False, server_default="1"),
        ),
        (
            "reimbursement",
            sa.Column("approved_level", sa.Integer(), nullable=False, server_default="0"),
        ),
        ("approval_log", sa.Column("level", sa.Integer(), nullable=True)),
        ("invoice", sa.Column("created_by", sa.String(64), nullable=True)),
    ]
    for table, column in adds:
        if not _has_column(bind, table, column.name):
            op.add_column(table, column)

    # 3) 缺索引补齐
    for table, name, col, unique in [
        ("reimbursement", "ix_reimbursement_approver_id", "approver_id", False),
        ("invoice", "ix_invoice_created_by", "created_by", False),
    ]:
        if _has_column(bind, table, col) and not _has_index(bind, table, name):
            op.create_index(name, table, [col], unique=unique)


def downgrade() -> None:
    """不删表也不删字段。

    downgrade 意味着「回到 v1」，而 v1 本来就缺这些结构，删掉会让仍在跑的 v2
    代码立刻报错。留在这里不动，比制造一个不可用的库更安全。
    """
    pass
