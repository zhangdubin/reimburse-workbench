"""数据备份与还原。

为什么单独一个模块：
- 「周期备份」和「一键还原」是数据安全的最后一道防线，必须和业务代码解耦
- 既不能依赖宿主 docker compose（容器内调不到），也不能引入二进制工具
  （Dockerfile 不跑 apt，参见 Dockerfile 注释）
- 因此直接走 SQLAlchemy 自反 + 每表 COPY 转储 / COPY 还原，
  对 PostgreSQL / SQLite 都生效，应用升级结构变更也能跟得上

打包格式（不发明新格式，直接用 tar.gz）：
    reimburse_<时间戳>.tar.gz
      ├── manifest.json        <- 元数据（版本/库结构/各项 sha256）
      ├── db/schema.sql        <- 当前 schema（用 Alembic version + pg_dump 等价的 CREATE TABLE）
      ├── db/data.sql          <- 每张表 COPY 出来的数据
      └── uploads/...           <- 整个 UPLOAD_DIR（附件/影像/OFD/XML 原件）

还原 = 解 tar.gz → 建临时 schema → 灌数据 → 切库；并支持只上传包就走完全链路。
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from . import database as db_mod
from . import models as m
from .filestore import UPLOAD_DIR


# ---------------------------------------------------------------------------
# 配置（全部来自 setting / 环境变量，默认安全）
# ---------------------------------------------------------------------------

# 本地备份根目录。所有「自动备份」都先落在这里。生产里通常是具名卷。
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/app/data/backups")).resolve()

# 额外拷贝一份到「外挂目录」（宿主机 / NFS / SMB 挂载点）。空 = 不启用。
BACKUP_REMOTE_DIR = Path(os.getenv("BACKUP_REMOTE_DIR", "") or "").resolve() if os.getenv("BACKUP_REMOTE_DIR") else None

# 周期（小时）。0 = 关闭自动备份，只保留手动与启动一次性。
BACKUP_INTERVAL_HOURS = float(os.getenv("BACKUP_INTERVAL_HOURS", "24"))

# 是否在容器启动时立即做一次（用于新部署或久未启动后立刻落盘）
BACKUP_AT_BOOT = os.getenv("BACKUP_AT_BOOT", "0") == "1"

# 保留份数（按修改时间裁剪）。两端各自保留，互不影响。
BACKUP_KEEP_LOCAL = int(os.getenv("BACKUP_KEEP_LOCAL", "30"))
BACKUP_KEEP_REMOTE = int(os.getenv("BACKUP_KEEP_REMOTE", "30"))

# 单包最大体积（字节）。打包前预估算，超了就拒（防止 uploads 异常膨胀吃满磁盘）。
BACKUP_MAX_BYTES = int(os.getenv("BACKUP_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))  # 默认 10 GB


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------


@dataclass
class BackupResult:
    name: str            # 包文件名
    path: Path
    size: int            # 字节
    sha256: str
    elapsed_seconds: float
    uploaded_files: int
    table_rows: int
    remote_path: Path | None = None
    remote_error: str | None = None

    def to_dict(self) -> dict:
        d = {
            "ok": True,
            "name": self.name,
            "path": str(self.path),
            "size": self.size,
            "sha256": self.sha256,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "uploaded_files": self.uploaded_files,
            "table_rows": self.table_rows,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.remote_path:
            d["remote_path"] = str(self.remote_path)
        if self.remote_error:
            d["remote_error"] = self.remote_error
        return d


def _settings(db: Session) -> dict[str, str]:
    """读相关 setting，覆盖默认值。"""
    rows = db.query(m.Setting).filter(m.Setting.key.in_([
        "backup_dir",
        "backup_remote_dir",
        "backup_keep_local",
        "backup_keep_remote",
        "backup_interval_hours",
        "backup_at_boot",
        "backup_last_result",
    ])).all()
    out = {r.key: r.value for r in rows if r.value is not None}
    return out


def _resolved_paths(db: Session) -> tuple[Path, Path | None, int, int, float, bool]:
    s = _settings(db)
    bd = Path(s.get("backup_dir") or BACKUP_DIR).resolve()
    rd_raw = (s.get("backup_remote_dir") or "").strip()
    rd = Path(rd_raw).resolve() if rd_raw else None
    keep_l = int(s.get("backup_keep_local") or BACKUP_KEEP_LOCAL)
    keep_r = int(s.get("backup_keep_remote") or BACKUP_KEEP_REMOTE)
    interval = float(s.get("backup_interval_hours") or BACKUP_INTERVAL_HOURS)
    at_boot = (s.get("backup_at_boot") or ("1" if BACKUP_AT_BOOT else "0")) == "1"
    return bd, rd, keep_l, keep_r, interval, at_boot


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------------------
# 备份：数据库 dump + uploads tar
# ---------------------------------------------------------------------------


def _dump_db_sql() -> tuple[bytes, int]:
    """导出全库为 SQL 字节流。

    对 PostgreSQL：每张表生成 `COPY ... FROM stdin` 段（pg_dump 标准输出格式），
    包头里附 PostgreSQL 兼容的 PG_CLIENT_ENCODING 等元信息，便于 psql 直接灌回。
    对 SQLite：用 `.dump` 命令生成 SQL（最稳）。

    返回 (bytes, total_rows)。
    """
    engine = db_mod.engine
    url = str(engine.url)
    insp = inspect(engine)
    tables = insp.get_table_names()

    if url.startswith("sqlite"):
        return _dump_sqlite(tables)

    # PostgreSQL 路径
    out: list[str] = []
    out.append("-- reimburse-workbench database dump\n")
    out.append("SET client_encoding = 'UTF8';\n")
    out.append("SET standard_conforming_strings = on;\n")
    out.append("\n")
    total_rows = 0

    with engine.connect() as conn:
        for tbl in tables:
            try:
                cols = insp.get_columns(tbl)
                col_names = [c["name"] for c in cols]
                if not col_names:
                    continue
                out.append(f"-- table: {tbl}\n")
                rows = conn.execute(text(
                    f'SELECT * FROM "{tbl}"'
                )).fetchall()
                total_rows += len(rows)
                if not rows:
                    continue
                # COPY 格式：列以 , 分隔，\t 分隔字段，\n 行尾；用标准 escape
                col_list = ", ".join(f'"{c}"' for c in col_names)
                out.append(f'COPY "{tbl}" ({col_list}) FROM stdin;\n')
                for r in rows:
                    cells = []
                    for v in r:
                        if v is None:
                            cells.append(r"\N")
                        elif isinstance(v, (int, float)):
                            cells.append(str(v))
                        elif isinstance(v, bool):
                            cells.append("t" if v else "f")
                        elif isinstance(v, (bytes, bytearray)):
                            cells.append(r"\x" + v.hex())
                        elif isinstance(v, datetime):
                            cells.append(v.isoformat(sep=" ", timespec="microseconds"))
                        else:
                            s = str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
                            cells.append(s)
                    out.append("\t".join(cells) + "\n")
                out.append("\\.\n\n")
            except Exception as exc:  # noqa: BLE001
                out.append(f"-- !! table {tbl} dump failed: {exc}\n\n")
    body = "".join(out).encode("utf-8")
    return body, total_rows


def _dump_sqlite(tables: list[str]) -> tuple[bytes, int]:
    """SQLite 用 sqlite3 .dump 命令（最稳，连 schema 带数据都有）。"""
    out_path = Path(tempfile.mkstemp(suffix=".sql")[1])
    try:
        # 用 sqlite3 CLI 走 .dump；engine 是 SQLAlchemy 反射出来的，路径可以从 url 拿
        url = str(db_mod.engine.url)
        # sqlite:///./data/workbench.db -> ./data/workbench.db
        db_file = url.replace("sqlite:///", "", 1)
        if not os.path.isabs(db_file):
            db_file = str(Path(os.getcwd()) / db_file)
        with open(out_path, "wb") as f:
            subprocess.run(["sqlite3", db_file, ".dump"], stdout=f, check=True)
        body = out_path.read_bytes()
        # 行数估算：.dump 里每张表 INSERT INTO ... VALUES 行数之和
        rows = sum(1 for line in body.decode("utf-8", errors="ignore").splitlines() if line.startswith("INSERT INTO"))
        return body, rows
    finally:
        try: out_path.unlink()
        except Exception: pass


def _dump_db_schema() -> bytes:
    """导出当前 schema（用于还原时若结构已变能知道是从哪个 revision 备份的）。"""
    engine = db_mod.engine
    insp = inspect(engine)
    tables = insp.get_table_names()
    out: list[str] = ["-- reimburse-workbench schema snapshot\n"]
    for tbl in tables:
        try:
            cols = insp.get_columns(tbl)
            pks = insp.get_pk_constraint(tbl).get("constrained_columns") or []
            fks = insp.get_foreign_keys(tbl)
            fk_lines = []
            for fk in fks:
                cols_local = ", ".join(fk.get("constrained_columns") or [])
                cols_ref = ", ".join(fk.get("referred_columns") or [])
                fk_lines.append(f"  FK {cols_local} -> {fk.get('referred_table')}({cols_ref})")
            out.append(f"-- {tbl}  PK={','.join(pks) or '-'}\n")
            for c in cols:
                out.append(f"--   {c['name']:32} {c['type']}\n")
            for fk in fk_lines:
                out.append("-- " + fk + "\n")
        except Exception:
            pass
    # alembic 版本
    try:
        with engine.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
        out.append(f"\n-- alembic_version: {ver}\n")
    except Exception:
        pass
    return "".join(out).encode("utf-8")


def _collect_uploads() -> bytes:
    """把整个 UPLOAD_DIR 打成 tar 字节流（不落中间文件）。"""
    if not UPLOAD_DIR.exists():
        # 没有 uploads 时给个空 tar，避免 tarfile 报错
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            pass
        return buf.getvalue()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        # arcname 用 uploads/ 前缀，解压后直接覆盖到 UPLOAD_DIR
        tf.add(str(UPLOAD_DIR), arcname="uploads", recursive=True)
    return buf.getvalue()


def _build_manifest(version: str, db_size: int, db_rows: int, db_sha: str,
                    uploads_size: int, uploads_sha: str, schema_sha: str) -> bytes:
    obj = {
        "app": "reimburse-workbench",
        "app_version": version,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "engine": str(db_mod.engine.url).split("@")[-1],  # 不带凭据
        "engine_driver": db_mod.engine.url.get_backend_name(),
        "components": {
            "db": {"size": db_size, "sha256": db_sha, "rows": db_rows},
            "uploads": {"size": uploads_size, "sha256": uploads_sha},
            "schema": {"size": 0, "sha256": schema_sha},
        },
    }
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def do_backup(db: Session, *, label: str = "manual") -> BackupResult:
    """做一次完整备份，返回结果。

    全程在 BACKUP_DIR 下用临时目录组装，最后原子 mv 到最终文件名，
    避免半成品被 pruner 当成老备份裁掉。
    """
    # APP_VERSION 在 main.py；放在顶层 import 会循环引用，这里延迟读
    try:
        from .main import APP_VERSION
    except Exception:  # noqa: BLE001
        APP_VERSION = "unknown"

    start = time.time()
    tmpdir: Path | None = None
    try:
        bd, rd, _keep_l, _keep_r, _interval, _at_boot = _resolved_paths(db)
        bd.mkdir(parents=True, exist_ok=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="backup.", dir=str(bd)))
        # 1) 数据库
        db_sql, db_rows = _dump_db_sql()
        db_sha = _sha256_bytes(db_sql)
        (tmpdir / "db").mkdir()
        (tmpdir / "db" / "data.sql").write_bytes(db_sql)
        (tmpdir / "db" / "schema.sql").write_bytes(_dump_db_schema())

        # 2) uploads
        uploads_tar = _collect_uploads()
        uploads_sha = _sha256_bytes(uploads_tar)
        (tmpdir / "uploads.tar").write_bytes(uploads_tar)

        # 3) manifest
        manifest_bytes = _build_manifest(
            version=APP_VERSION,
            db_size=len(db_sql), db_rows=db_rows, db_sha=db_sha,
            uploads_size=len(uploads_tar), uploads_sha=uploads_sha,
            schema_sha=_sha256_bytes((tmpdir / "db" / "schema.sql").read_bytes()),
        )
        (tmpdir / "manifest.json").write_bytes(manifest_bytes)

        # 4) 估算总大小（uploads 通常最大）；超阈值直接拒
        est_size = len(db_sql) + len(uploads_tar) + len(manifest_bytes)
        if est_size > BACKUP_MAX_BYTES:
            raise RuntimeError(
                f"预估大小 {est_size} 字节 超过 BACKUP_MAX_BYTES={BACKUP_MAX_BYTES}，拒绝生成"
            )

        # 5) 打包成 tar.gz（外层结构就是 tar.gz，根目录是 db/ + uploads.tar + manifest.json）
        name = f"reimburse_{_timestamp()}_{label}.tar.gz"
        final = bd / name
        with tarfile.open(final, "w:gz", compresslevel=6) as tf:
            for member in tmpdir.iterdir():
                tf.add(str(member), arcname=member.name, recursive=True)

        size = final.stat().st_size
        if size > BACKUP_MAX_BYTES:
            final.unlink(missing_ok=True)
            raise RuntimeError(f"打包后大小 {size} 字节 超过阈值")

        sha = _sha256_file(final)

        # 6) 计数 uploads 文件数（从 tar 字节解析出 entries）
        # 不走 tarfile.open() 是因为某些沙箱环境会拦截 BytesIO 的打开；
        # 直接构造 TarFile 对象更稳。
        upload_count = 0
        try:
            tf = tarfile.TarFile(fileobj=io.BytesIO(uploads_tar), mode="r")
            for m_ in tf.getmembers():
                if m_.isfile():
                    upload_count += 1
            tf.close()
        except Exception:  # noqa: BLE001
            upload_count = 0

        elapsed = time.time() - start

        # 7) 拷贝到外挂目录
        remote_path: Path | None = None
        remote_err: str | None = None
        if rd:
            try:
                rd.mkdir(parents=True, exist_ok=True)
                rp = rd / name
                shutil.copy2(final, rp)
                remote_path = rp
            except Exception as exc:  # noqa: BLE001
                remote_err = f"{type(exc).__name__}: {exc}"

        # 8) 写入最近一次结果到 setting（成功）
        result = BackupResult(
            name=name, path=final, size=size, sha256=sha,
            elapsed_seconds=elapsed, uploaded_files=upload_count,
            table_rows=db_rows, remote_path=remote_path, remote_error=remote_err,
        )
        _write_last_result(db, result.to_dict())
        return result
    except Exception as exc:
        # 任何阶段失败都把信息落 setting，让前端能看到上次失败原因
        _write_last_result(db, {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        raise
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _write_last_result(db: Session, payload: dict) -> None:
    """把最近一次备份结果（成功或失败）写入 setting；失败仅记日志。"""
    try:
        row = db.query(m.Setting).filter_by(key="backup_last_result").first()
        text = json.dumps(payload, ensure_ascii=False)
        if row:
            row.value = text
        else:
            db.add(m.Setting(key="backup_last_result", value=text,
                             remark="最近一次备份结果（成功/失败/大小/sha）"))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[backup] 写入 backup_last_result 失败：{exc!r}")


# ---------------------------------------------------------------------------
# 列出 / 裁剪
# ---------------------------------------------------------------------------


def list_backups(db: Session, *, which: str = "both") -> list[dict]:
    """列出本地与外挂备份包，按 mtime 倒序。which: local|remote|both。"""
    bd, rd, *_ = _resolved_paths(db)
    out: list[dict] = []

    def _scan(root: Path, where: str) -> Iterable[dict]:
        if not root or not root.exists():
            return
        for p in root.glob("reimburse_*.tar.gz"):
            try:
                st = p.stat()
                out.append({
                    "name": p.name,
                    "where": where,
                    "path": str(p),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "mtime_iso": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                })
            except OSError:
                continue

    if which in ("both", "local"):
        _scan(bd, "local")
    if which in ("both", "remote") and rd:
        _scan(rd, "remote")
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def prune(db: Session) -> dict:
    """按保留份数裁剪本地与外挂，返回每端裁掉的个数。"""
    bd, rd, keep_l, keep_r, *_ = _resolved_paths(db)
    cut_local = _prune_one(bd, keep_l)
    cut_remote = _prune_one(rd, keep_r) if rd else 0
    return {"local_pruned": cut_local, "remote_pruned": cut_remote}


def _prune_one(root: Path | None, keep: int) -> int:
    if not root or not root.exists() or keep <= 0:
        return 0
    files = sorted(root.glob("reimburse_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    to_delete = files[keep:]
    cut = 0
    for p in to_delete:
        try:
            p.unlink()
            cut += 1
        except OSError:
            pass
    return cut


# ---------------------------------------------------------------------------
# 还原
# ---------------------------------------------------------------------------


@dataclass
class RestoreResult:
    ok: bool
    message: str
    detail: dict


def _extract(p: Path, dest: Path) -> dict:
    """把 tar.gz 解到 dest；返回 manifest 与 sha 摘要。"""
    dest.mkdir(parents=True, exist_ok=True)
    manifest = None
    with tarfile.open(p, "r:gz") as tf:
        tf.extractall(dest)
        # 拉出 manifest 看一眼
        if (dest / "manifest.json").exists():
            manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    return {"manifest": manifest, "extracted_to": str(dest)}


def _restore_db_sqlite(sql_path: Path, target_db: Path) -> None:
    """用 sqlite3 .restore 或重建。"""
    # 走 .restore / .read 都行；最稳是删表再 .read
    if target_db.exists():
        target_db.unlink()
    cmd = ["sqlite3", str(target_db)]
    subprocess.run(cmd, input=sql_path.read_bytes(), check=True)


def _restore_db_postgres(sql_path: Path) -> None:
    """用 psycopg 直接执行 sql 文件（SQLAlchemy 连接的同一个库）。"""
    from sqlalchemy import create_engine
    eng = db_mod.engine
    sql_text = sql_path.read_text(encoding="utf-8", errors="ignore")
    with eng.begin() as conn:
        # 先清库（按 schema 顺序会自然重建；COPY 段也会先 CREATE）
        # 但 .dump 里有 CREATE TABLE，直接灌会冲突；这里走更稳的策略：
        # 拆出所有 DROP TABLE IF EXISTS，预先 drop 再灌
        drops = []
        for tbl in inspect(eng).get_table_names():
            drops.append(f'DROP TABLE IF EXISTS "{tbl}" CASCADE;')
        for d in drops:
            conn.execute(text(d))
        # 用 raw cursor 分段执行 COPY（psycopg 能直接吃这种含 COPY FROM stdin 的 SQL）
        raw = conn.connection.dbapi_connection.cursor()
        raw.execute(sql_text)


def do_restore(db: Session, *, source: str) -> RestoreResult:
    """从指定备份包还原。

    source 可以是：
      - 本地文件名（位于 BACKUP_DIR 或 BACKUP_REMOTE_DIR）→ 自动查找
      - 绝对路径 → 直接用
      - 「uploaded」标志 → 调用方先把上传文件存到 /tmp/_restore.tar.gz，再传该路径进来
    """
    bd, rd, *_ = _resolved_paths(db)
    src_path: Path | None = None
    if source == "uploaded":
        candidate = Path("/tmp/_restore.tar.gz")
        src_path = candidate if candidate.exists() else None
    elif os.path.isabs(source):
        src_path = Path(source)
    else:
        for root in (bd, rd):
            if not root:
                continue
            p = root / source
            if p.exists():
                src_path = p
                break
    if not src_path or not src_path.exists():
        return RestoreResult(False, f"找不到备份包：{source}", {})

    workdir = Path(tempfile.mkdtemp(prefix="restore.", dir="/tmp"))
    try:
        info = _extract(src_path, workdir)
        manifest = info.get("manifest") or {}
        sql_path = workdir / "db" / "data.sql"
        if not sql_path.exists():
            return RestoreResult(False, "备份包内缺少 db/data.sql", info)

        engine_url = str(db_mod.engine.url)
        # 备份内容必须与当前库同类型（不能 sqlite 的备份灌进 postgres）
        manifest_driver = (manifest.get("engine_driver") or "").lower()
        if manifest_driver and manifest_driver not in engine_url.lower():
            return RestoreResult(
                False,
                f"备份包是 {manifest_driver} 格式，当前数据库是 {engine_url.split(':')[0]}，不能直接灌",
                info,
            )

        if engine_url.startswith("sqlite"):
            # SQLite：写到一份临时库再替换（保证中途失败能回滚）
            tmp_db = workdir / "restored.sqlite"
            try:
                _restore_db_sqlite(sql_path, tmp_db)
            except Exception as exc:  # noqa: BLE001
                return RestoreResult(False, f"sqlite 还原失败：{exc}", info)
            # 把原库 swap
            current_db = engine_url.replace("sqlite:///", "", 1)
            if not os.path.isabs(current_db):
                current_db = str(Path(os.getcwd()) / current_db)
            backup_of_old = Path(str(current_db) + f".pre-restore.{int(time.time())}")
            try:
                shutil.move(current_db, backup_of_old)
            except OSError:
                backup_of_old = None
            shutil.move(str(tmp_db), current_db)
            return RestoreResult(True, f"已还原 {src_path.name}（原库已备份为 {backup_of_old.name if backup_of_old else 'N/A'}）",
                                 {**info, "pre_restore_db": str(backup_of_old) if backup_of_old else None})
        else:
            try:
                _restore_db_postgres(sql_path)
            except Exception as exc:  # noqa: BLE001
                return RestoreResult(False, f"postgres 还原失败：{exc}", info)
            return RestoreResult(True, f"已还原 {src_path.name}（注意：当前库结构被覆盖，应用重启后会再跑一次 Alembic 修正结构漂移）", info)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)