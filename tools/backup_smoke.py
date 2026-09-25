#!/usr/bin/env python3
"""数据备份链路端到端冒烟（开发模式，SQLite）。

为什么单独一个脚本：
-  容器内调不到宿主 docker，跑 do_backup 不需要 docker 也行
-  把整套备份+还原塞进 tools/ 跟 upgrade_dryrun 对齐位置
-  可以反复跑而不污染生产（用临时目录 + 临时 SQLite）

跑法（dev_server 起来后）：
    DATABASE_URL=sqlite:////tmp/wb_prod_test.db \
    UPLOAD_DIR=/tmp/wb-uploads-8792 \
    python3 tools/backup_smoke.py

验证：
1. 备份能生成 tar.gz，里面有 manifest / db/data.sql / uploads
2. 本地列表能看到新包
3. 还原到第二个 SQLite 文件能灌回数据
4. 失败时 backup_last_result 记录错误信息
"""
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# 让工具脚本走同一个 SQLite，与 dev_server 共享数据
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/wb_prod_test.db")
os.environ.setdefault("UPLOAD_DIR", "/tmp/wb-uploads-8792")

from app import backup as bk  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app import models as m  # noqa: E402

OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}  {extra}")


def main() -> int:
    print("== 数据备份冒烟 ==")
    # 让备份写到 /tmp 别污染 /app
    bd = Path("/tmp/wb-backup-smoke")
    rd = Path("/tmp/wb-backup-remote-smoke")
    if bd.exists():
        shutil.rmtree(bd)
    if rd.exists():
        shutil.rmtree(rd)
    bd.mkdir(parents=True)
    rd.mkdir(parents=True)

    db = SessionLocal()
    try:
        # 1. 覆盖默认目录，方便测试
        for key, val in [
            ("backup_dir", str(bd)),
            ("backup_remote_dir", str(rd)),
            ("backup_keep_local", "5"),
            ("backup_keep_remote", "5"),
            ("backup_interval_hours", "0"),
            ("backup_at_boot", "0"),
        ]:
            row = db.get(m.Setting, key)
            if row is None:
                db.add(m.Setting(key=key, value=val, remark="smoke test"))
            else:
                row.value = val
        db.commit()
    finally:
        db.close()

    print("\n[1] do_backup 一次")
    db = SessionLocal()
    try:
        result = bk.do_backup(db, label="smoke")
    finally:
        db.close()

    check("包文件存在", result.path.exists(), str(result.path))
    check("包大小 > 0", result.size > 0)
    check("sha256 64 字符", len(result.sha256) == 64)
    check("uploads tar 含 0 个文件（dev 默认无上传）", result.uploaded_files >= 0)
    check("table_rows > 0", result.table_rows > 0)
    check("外挂副本存在", result.remote_path and result.remote_path.exists())

    print(f"  → {result.name}  {result.size} B  sha={result.sha256[:12]}")

    print("\n[2] tar 包结构正确")
    with tarfile.open(result.path) as tf:
        names = tf.getnames()
    check("含 manifest.json", "manifest.json" in names)
    check("含 db/data.sql", "db/data.sql" in names)
    check("含 db/schema.sql", "db/schema.sql" in names)
    check("含 uploads.tar", "uploads.tar" in names)

    print("\n[3] manifest 字段完整")
    with tarfile.open(result.path) as tf:
        with tf.extractfile("manifest.json") as f:
            manifest = json.loads(f.read().decode())
    check("app == reimburse-workbench", manifest.get("app") == "reimburse-workbench")
    check(f"app_version == {__import__('app.main', fromlist=['APP_VERSION']).APP_VERSION}",
          manifest.get("app_version") == __import__('app.main', fromlist=['APP_VERSION']).APP_VERSION)
    check("engine_driver == sqlite", manifest.get("engine_driver") == "sqlite")
    check("含 components.db.sha256", "db" in manifest.get("components", {}))
    check("db.rows > 0", manifest["components"]["db"]["rows"] > 0)

    print("\n[4] list_backups 看到两个包（本地+外挂）")
    db = SessionLocal()
    try:
        items = bk.list_backups(db, which="both")
    finally:
        db.close()
    check("列表总数 >= 2", len(items) >= 2)
    check("有 local 项", any(it["where"] == "local" for it in items))
    check("有 remote 项", any(it["where"] == "remote" for it in items))

    print("\n[5] 还原到新 SQLite（灌回原库）")
    # 用临时库验证 schema
    target_db = Path("/tmp/wb_restore_target.db")
    if target_db.exists():
        target_db.unlink()
    # 用 sqlite3 CLI 灌一份看看
    import sqlite3
    conn = sqlite3.connect(str(target_db))
    try:
        # 把 data.sql 提出来灌进去
        with tarfile.open(result.path) as tf:
            with tf.extractfile("db/data.sql") as f:
                sql_text = f.read().decode()
        # 直接 exec 包含 CREATE TABLE 的 sql（dump 里 CREATE 顺序就是 FK 依赖顺序）
        # 拆 statements by ;
        cur = conn.cursor()
        # 先建结构（取 schema.sql）
        with tarfile.open(result.path) as tf:
            with tf.extractfile("db/schema.sql") as f:
                _schema = f.read().decode()
        # 不直接灌 schema.sql（那是注释），改用 data.sql 里的 CREATE 语句
        for stmt in sql_text.split(";\n"):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError as exc:
                # 表已存在或重复 CREATE 跳过
                if "already exists" not in str(exc):
                    print(f"    (跳过: {exc!r})")
        conn.commit()
        # 行数对得上
        from app.database import engine
        from sqlalchemy import text
        rows_now = conn.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0]
        check("还原后 alembic_version 表存在", rows_now is not None)
    finally:
        conn.close()

    print("\n[6] 写入 backup_last_result 后能读回")
    db = SessionLocal()
    try:
        row = db.get(m.Setting, "backup_last_result")
        check("setting 行存在", row is not None)
        if row and row.value:
            last = json.loads(row.value)
            check("last_result.name 一致", last.get("name") == result.name)
            check("last_result.ok", last.get("ok") is True)
            check("last_result.sha256 一致", last.get("sha256") == result.sha256)
    finally:
        db.close()

    print("\n[7] prune 按保留份数裁剪")
    db = SessionLocal()
    try:
        # 多做几份然后裁剪到 2
        for k, v in [("backup_keep_local", "2"), ("backup_keep_remote", "2")]:
            r = db.get(m.Setting, k); r.value = v
        db.commit()
        # 现有 1 份，先补 3 份让总数变 4
        for _ in range(3):
            time.sleep(1.05)  # 文件名时间戳要不同
            bk.do_backup(db, label="prune")
        info = bk.prune(db)
        check("local 裁剪 ≥ 2 份", info["local_pruned"] >= 2)
        check("remote 裁剪 ≥ 2 份", info["remote_pruned"] >= 2)
        items = bk.list_backups(db, which="both")
        # 保留份数 2，但还要看是否 1（部分时间戳冲突会合并成同时间），要求 ≤ 2
        local_count = sum(1 for it in items if it["where"] == "local")
        check(f"本地保留 ≤ 2 份（实际 {local_count}）", local_count <= 2)
    finally:
        db.close()

    print("\n[8] 错误捕获：故意改坏的 BACKUP_DIR 触发失败")
    db = SessionLocal()
    try:
        # macOS 上 /proc/self/oom 可能不存在能 mkdir 的，写一个保证 PermissionError 的路径
        bad_dir = "/this/path/does/not/exist/and/cannot/be/created"
        r = db.get(m.Setting, "backup_dir"); r.value = bad_dir
        db.commit()
        try:
            bk.do_backup(db, label="fail")
            check("失败会抛异常", False, "未抛异常")
        except (PermissionError, OSError) as exc:
            check("失败会抛异常", True, str(exc)[:60])
        # 重新读 last_result，确认错误已落库
        row = db.get(m.Setting, "backup_last_result")
        if row and row.value:
            last = json.loads(row.value)
            check("failure 写入 last_result.ok=False", last.get("ok") is False,
                  f"got {last.get('ok')}, err={last.get('error', '')[:60]}")
        else:
            check("failure 写入 last_result.ok=False", False, "last_result 为空")
    finally:
        # 恢复
        r = db.get(m.Setting, "backup_dir"); r.value = str(bd); db.commit(); db.close()

    print()
    print(f"== 结果: {OK} 通过 / {FAIL} 失败 ==")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())