"""数据库连接与 Session 管理。

DATABASE_URL 约定：
  - 开发/单机：sqlite:///./data/workbench.db
  - 生产/Docker：postgresql+psycopg://user:pass@db:5432/workbench
"""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/workbench.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# SQLite 需要确保目录存在
if DATABASE_URL.startswith("sqlite:///"):
    _db_path = Path(DATABASE_URL.replace("sqlite:///", "", 1))
    if not _db_path.is_absolute():
        _db_path = Path.cwd() / _db_path
    _db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
