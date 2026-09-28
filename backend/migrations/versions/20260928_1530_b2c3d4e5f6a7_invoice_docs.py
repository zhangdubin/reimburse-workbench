"""发票凭证类型 + 费用类型应附单据（v2.9.22）

背景：发票之外还需要一类「佐证材料」——市内交通（滴滴等）要行程单、
住宿要消费水单（folio）。这些材料的归属天然落在发票上，
所以不新建实体表，直接给发票附件加类型：

invoice_attachment:
  kind    凭证类型，invoice=发票影像 / itinerary=行程单 /
          folio=消费水单 / other=其他材料。默认 invoice，历史数据自动归为发票影像。

expense_category:
  required_doc  应附单据要求（itinerary/folio/other），NULL=不作要求。
                列表页据此提示「缺行程单 / 缺水单」。

回填：名称含「市内交通」的费用类型要求行程单；含「住宿」的要求消费水单。
这两条是最典型的场景，其余由财务在基础数据里自行维护。

不进 app.models：表结构字面量写死。
可重复执行：加列前先查列，重复迁移是空操作。

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


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

    # ---------- invoice_attachment.kind ----------
    att_cols = _cols(bind, "invoice_attachment")
    if "kind" not in att_cols:
        with op.batch_alter_table("invoice_attachment") as b:
            b.add_column(sa.Column("kind", sa.String(16), nullable=False,
                                   server_default="invoice"))
        # 历史附件全部是发票影像，server_default 已把它们补齐，无需额外 UPDATE

    # ---------- expense_category.required_doc ----------
    cat_cols = _cols(bind, "expense_category")
    if "required_doc" not in cat_cols:
        with op.batch_alter_table("expense_category") as b:
            b.add_column(sa.Column("required_doc", sa.String(16), nullable=True))
        # 典型场景回填：只动明显能匹配上的，其余留给财务自己维护
        bind.execute(sa.text(
            "UPDATE expense_category SET required_doc = 'itinerary' "
            "WHERE required_doc IS NULL AND name LIKE '%市内交通%'"
        ))
        bind.execute(sa.text(
            "UPDATE expense_category SET required_doc = 'folio' "
            "WHERE required_doc IS NULL AND name LIKE '%住宿%'"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if "required_doc" in _cols(bind, "expense_category"):
        with op.batch_alter_table("expense_category") as b:
            b.drop_column("required_doc")
    if "kind" in _cols(bind, "invoice_attachment"):
        with op.batch_alter_table("invoice_attachment") as b:
            b.drop_column("kind")
