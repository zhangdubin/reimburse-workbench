"""Alembic 运行环境。

连接串统一从环境变量 DATABASE_URL 读取（与 app/database.py 同一套约定），
所以 `alembic upgrade head` 与容器启动用的是同一个库，不会出现「迁移跑到别的库上」。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 env.py 能 import 到 app 包
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.database import Base, DATABASE_URL  # noqa: E402
from app import models  # noqa: E402,F401  必须导入，autogenerate 才能看到全部表

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=DATABASE_URL.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # 不走 alembic configparser 写 sqlalchemy.url——密码里有 % 或 @ 会被
    # configparser 当插值或被 urlparse 当分隔符错位（v2.9.16 实战验证过）。
    # 直接用 app 已经 create_engine 好的实例，连过来跑迁移。
    from app.database import engine
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite 不支持大部分 ALTER，批处理模式下会自动走「建新表 -> 拷数据 -> 改名」
            render_as_batch=DATABASE_URL.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
