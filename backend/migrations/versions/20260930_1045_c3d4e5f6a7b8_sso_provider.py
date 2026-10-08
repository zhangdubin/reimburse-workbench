"""单点登录（v2.9.23）

统一门户对接：一个身份源一条配置，管理员在「单点登录」页维护，不改代码不重启。

sso_provider:
  协议（oauth2 / oidc / cas3 / jwt）+ 端点 + 凭据 + 字段映射 + 落地策略。
  client_secret_enc / jwt_secret_enc 落的是密文（app/crypt.py），库泄露不等于门户凭据泄露。

sso_login_state:
  登录过程态。pending=等门户回调，ready=等前端换 token。
  一次性 + 有时效，避免把用户资料塞进 URL。

app_user:
  auth_source      local / sso
  sso_provider_id  归属的身份源
  sso_subject      门户侧唯一标识，(provider, subject) 唯一约束兜住重复开户

既有本地账号全部保持 auth_source=local，行为完全不变——
SSO 没配任何身份源时，登录页仍只出现账密表单。

不进 app.models：表结构字面量写死。
可重复执行：建表/加列前先查，重复迁移是空操作。

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _cols(bind, table: str) -> set[str]:
    """读出指定表已有的列名，跨 SQLite / PostgreSQL。"""
    if bind.dialect.name == "sqlite":
        rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return {r[1] for r in rows}
    rows = bind.execute(sa.text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
    ), {"t": table}).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    # ---------- sso_provider ----------
    if "sso_provider" not in tables:
        op.create_table(
            "sso_provider",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(64), nullable=False, unique=True),
            sa.Column("protocol", sa.String(16), nullable=False, server_default="oidc"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("issuer", sa.String(255), nullable=True),
            sa.Column("authorize_url", sa.Text(), nullable=True),
            sa.Column("token_url", sa.Text(), nullable=True),
            sa.Column("userinfo_url", sa.Text(), nullable=True),
            sa.Column("validate_url", sa.Text(), nullable=True),
            sa.Column("jwks_url", sa.Text(), nullable=True),
            sa.Column("logout_url", sa.Text(), nullable=True),
            sa.Column("client_id", sa.String(255), nullable=True),
            sa.Column("client_secret_enc", sa.Text(), nullable=True),
            sa.Column("scope", sa.String(255), nullable=True, server_default="openid profile email"),
            sa.Column("use_pkce", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("jwt_source", sa.String(16), nullable=True, server_default="query"),
            sa.Column("jwt_param", sa.String(64), nullable=True, server_default="token"),
            sa.Column("jwt_secret_enc", sa.Text(), nullable=True),
            sa.Column("jwt_audience", sa.String(255), nullable=True),
            sa.Column("claim_subject", sa.String(64), nullable=False, server_default="sub"),
            sa.Column("claim_username", sa.String(64), nullable=False, server_default="preferred_username"),
            sa.Column("claim_name", sa.String(64), nullable=False, server_default="name"),
            sa.Column("claim_employee_no", sa.String(64), nullable=True, server_default="employee_no"),
            sa.Column("claim_department", sa.String(64), nullable=True, server_default="department"),
            sa.Column("claim_email", sa.String(64), nullable=True, server_default="email"),
            sa.Column("claim_phone", sa.String(64), nullable=True, server_default="phone_number"),
            sa.Column("claim_groups", sa.String(64), nullable=True, server_default="groups"),
            sa.Column("auto_create", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("default_role", sa.String(16), nullable=False, server_default="申请人"),
            sa.Column("role_map", sa.Text(), nullable=True, server_default="{}"),
            sa.Column("sync_profile", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("allow_local_login", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("bind_local_by_username", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("session_hours", sa.Integer(), nullable=False, server_default="12"),
            sa.Column("verify_ssl", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("timeout_sec", sa.Integer(), nullable=False, server_default="8"),
            sa.Column("remark", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )
        # protocol / enabled 立索引：登录页每次都要筛 enabled
        op.create_index("ix_sso_provider_protocol", "sso_provider", ["protocol"])
        op.create_index("ix_sso_provider_enabled", "sso_provider", ["enabled"])

    # ---------- sso_login_state ----------
    if "sso_login_state" not in tables:
        op.create_table(
            "sso_login_state",
            sa.Column("nonce", sa.String(64), primary_key=True),
            sa.Column("provider_id", sa.Integer(),
                      sa.ForeignKey("sso_provider.id"), nullable=False),
            sa.Column("stage", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("pkce_verifier", sa.String(128), nullable=True),
            sa.Column("profile_json", sa.Text(), nullable=True),
            sa.Column("username_attempt", sa.String(64), nullable=True),
            sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_sso_login_state_provider_id", "sso_login_state", ["provider_id"])

    # ---------- user_session：记录 SSO 会话来源（单点登出用）----------
    sess_cols = _cols(bind, "user_session")
    if sess_cols and "sso_provider_id" not in sess_cols:
        op.add_column("user_session", sa.Column("sso_provider_id", sa.Integer(), nullable=True))

    # ---------- app_user 扩展 ----------
    user_cols = _cols(bind, "app_user")
    dialect = bind.dialect.name
    # 这里刻意不带 ForeignKey：
    #   SQLite 的 alembic 不支持 ALTER ... ADD CONSTRAINT（会抛 NotImplementedError），
    #   batch_alter_table 又会因为重建 app_user 里既有的无名外键而抛
    #   "Constraint must have a name" —— 两条路都走死。
    # 所以「先纯加列」，再只在 PostgreSQL 上补库级外键；SQLite（仅本机 dev）
    # 由 ORM 层的 ForeignKey 语义 + 应用层保证，SQLite 默认也不强制外键。
    new_cols = (
        ("auth_source", sa.Column("auth_source", sa.String(16), nullable=False, server_default="local")),
        ("sso_provider_id", sa.Column("sso_provider_id", sa.Integer(), nullable=True)),
        ("sso_subject", sa.Column("sso_subject", sa.String(128), nullable=True)),
    )
    missing = [(n, c) for n, c in new_cols if n not in user_cols]
    for _, col in missing:
        op.add_column("app_user", col)

    if dialect == "postgresql":
        try:
            op.create_foreign_key(
                "fk_app_user_sso_provider", "app_user", "sso_provider",
                ["sso_provider_id"], ["id"],
            )
        except Exception:  # noqa: BLE001  已存在（重复迁移）
            pass
        # (provider, subject) 唯一：门户侧同一个人多次回调不会开出两个号。
        # 新列全为 NULL，PG 下 NULL 互不相等，不会撞约束。
        try:
            op.create_unique_constraint(
                "uq_user_sso_bind", "app_user", ["sso_provider_id", "sso_subject"]
            )
        except Exception:  # noqa: BLE001
            pass

    if "auth_source" in [n for n, _ in missing]:
        try:
            op.create_index("ix_app_user_auth_source", "app_user", ["auth_source"])
        except Exception:  # noqa: BLE001
            pass


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    sess_cols = _cols(bind, "user_session")
    if "sso_provider_id" in sess_cols:
        op.drop_column("user_session", "sso_provider_id")

    user_cols = _cols(bind, "app_user")
    if bind.dialect.name != "sqlite":
        for name in ("uq_user_sso_bind",):
            try:
                op.drop_constraint(name, "app_user", type_="unique")
            except Exception:  # noqa: BLE001
                pass
        try:
            op.drop_index("ix_app_user_auth_source", table_name="app_user")
        except Exception:  # noqa: BLE001
            pass
    for col in ("sso_subject", "sso_provider_id", "auth_source"):
        if col in user_cols:
            op.drop_column("app_user", col)

    if "sso_login_state" in tables:
        op.drop_table("sso_login_state")
    if "sso_provider" in tables:
        op.drop_table("sso_provider")
