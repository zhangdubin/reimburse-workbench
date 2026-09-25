"""AI 智能：模型接入配置与调用流水

新增两张表：

- `ai_provider`：大模型接入配置（端点、模型、加密后的 API Key、场景开关、
  连通性测试结果）。API Key 用与邮箱密码同一套主密钥加密存储。
- `ai_usage`：每次调用的流水（场景、模型、token、耗时、成败），
  用于成本核算与「AI 为什么没生效」的排查。

刻意**不 import app.models**：迁移记录「当时发生了什么」，不该随模型演进而变。
表结构在这里用字面量写死，并且建表用 checkfirst，重复执行是空操作。

Revision ID: b7d4e1c9a2f3
Revises: 5f96e014a6eb
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7d4e1c9a2f3"
down_revision = "5f96e014a6eb"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table("ai_provider"):
        op.create_table(
            "ai_provider",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(64), nullable=False, unique=True),
            sa.Column("preset", sa.String(32), nullable=False, server_default="custom"),
            sa.Column("base_url", sa.String(255), nullable=False, server_default=""),
            sa.Column("api_key_enc", sa.String(1024), nullable=False, server_default=""),
            sa.Column("model", sa.String(128), nullable=False, server_default=""),
            sa.Column("vision_model", sa.String(128), nullable=True),
            sa.Column("auth_style", sa.String(16), nullable=False, server_default="bearer"),
            sa.Column("extra_headers", sa.Text(), nullable=True),
            sa.Column("extra_query", sa.Text(), nullable=True),
            sa.Column("temperature", sa.Numeric(3, 2), nullable=True, server_default="0.30"),
            sa.Column("max_tokens", sa.Integer(), nullable=True, server_default="2048"),
            sa.Column("timeout_sec", sa.Integer(), nullable=True, server_default="60"),
            sa.Column("use_assistant", sa.Boolean(), nullable=True, server_default=sa.true()),
            sa.Column("use_recognize", sa.Boolean(), nullable=True, server_default=sa.true()),
            sa.Column("use_analyze", sa.Boolean(), nullable=True, server_default=sa.true()),
            sa.Column("enabled", sa.Boolean(), nullable=True, server_default=sa.true()),
            sa.Column("is_default", sa.Boolean(), nullable=True, server_default=sa.false()),
            sa.Column("last_test_at", sa.DateTime(), nullable=True),
            sa.Column("last_test_ok", sa.Boolean(), nullable=True),
            sa.Column("last_test_detail", sa.Text(), nullable=True),
            sa.Column("remark", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )

    if not _has_table("ai_usage"):
        op.create_table(
            "ai_usage",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), nullable=True),
            sa.Column("username", sa.String(64), nullable=True),
            sa.Column("kind", sa.String(16), nullable=False, server_default="助手问答"),
            sa.Column("provider_id", sa.Integer(), sa.ForeignKey("ai_provider.id"), nullable=True),
            sa.Column("provider_name", sa.String(64), nullable=True),
            sa.Column("model", sa.String(128), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("latency_ms", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("ok", sa.Boolean(), nullable=True, server_default=sa.true()),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_ai_usage_user_id", "ai_usage", ["user_id"])
        op.create_index("ix_ai_usage_kind", "ai_usage", ["kind"])
        op.create_index("ix_ai_usage_ok", "ai_usage", ["ok"])
        op.create_index("ix_ai_usage_created_at", "ai_usage", ["created_at"])

    # 触发器 / 序列在 SQLite 与 PostgreSQL 上都不需要，建完即用
    _ = bind


def downgrade() -> None:
    """不做破坏性回滚：AI 配置与用量流水是运维资产，宁可留表也不误删。"""
    pass
