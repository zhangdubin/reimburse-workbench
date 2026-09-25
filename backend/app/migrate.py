"""把 Alembic 迁移包成可在启动时调用的函数。

生产环境不应依赖 `Base.metadata.create_all`：它只会「补建缺失的表」，
既不会改字段类型也不会加索引，表结构一旦演进就会悄悄和代码不一致。
统一走迁移后，升级只需 `alembic upgrade head`（容器启动时自动执行）。
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from .database import DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    # env.py 也会再读一次环境变量，这里显式设置保证优先级最高
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    return cfg


# v1 用 Base.metadata.create_all 建库，没有 alembic_version 表。
# 这种库直接 upgrade 会因为「表已存在」整条失败，所以先把它认成初始版本再往上走。
_LEGACY_PROBE_TABLE = "reimbursement"
_INITIAL_REVISION = "cc5660459504"


def _needs_legacy_stamp() -> bool:
    """判断是否是一个「已建表但没有迁移版本」的旧库。"""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as conn:
            if MigrationContext.configure(conn).get_current_revision():
                return False  # 已有版本号，正常库
            tables = set(inspect(conn).get_table_names())
    finally:
        engine.dispose()

    if not tables:
        return False  # 空库，正常从零迁移
    if _LEGACY_PROBE_TABLE not in tables:
        # 有表但没有业务表，说明不是本项目的老库，交给 Alembic 正常报错更安全
        return False
    return True


def stamp_legacy_if_needed() -> bool:
    """旧库（create_all 建的）先打上初始版本，再让 upgrade 往上补新表新字段。

    只在 `AUTO_STAMP_LEGACY != 0` 时生效，并且会打印醒目日志，便于出事时追溯。
    """
    if os.getenv("AUTO_STAMP_LEGACY", "1").lower() in ("0", "false", "no"):
        return False
    if not _needs_legacy_stamp():
        return False

    print("[migrate] 检测到 v1 旧库（有业务表但没有 alembic_version），")
    print(f"[migrate] 先标记为初始版本 {_INITIAL_REVISION}，再执行增量升级")
    command.stamp(alembic_config(), _INITIAL_REVISION)
    return True


def upgrade_to_head() -> bool:
    """执行到最新版本。AUTO_MIGRATE=0 时跳过（适合把迁移交给独立的发布流水线）。"""
    if os.getenv("AUTO_MIGRATE", "1").lower() in ("0", "false", "no"):
        print("[migrate] AUTO_MIGRATE=0，跳过自动迁移")
        return False
    try:
        stamp_legacy_if_needed()
    except Exception as exc:  # noqa: BLE001
        # 打标失败不致命：可能只是并发，继续走 upgrade 让 Alembic 给出真正的报错
        print(f"[migrate] 旧库标记失败（继续尝试常规升级）：{exc}")
    command.upgrade(alembic_config(), "head")
    print("[migrate] 数据库结构已升级到最新版本")
    return True


def current_revision() -> str | None:
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
