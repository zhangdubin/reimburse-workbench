"""销售费用报销管理工作台 - 后端入口。

生产化改造要点：
- 数据库结构走 Alembic 迁移，启动时自动 `upgrade head`
- 启动只做「参数补齐 + 初始管理员」，**不再自动灌演示数据**
- CORS 默认关闭（同源），需要跨域时用 CORS_ORIGINS 显式白名单
- 健康检查不暴露数据库连接串
- 全局异常兜底返回 JSON，不把堆栈吐给前端
"""

from __future__ import annotations

import asyncio
import csv
import io
import mimetypes
import os
import time
from contextlib import asynccontextmanager, suppress
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from . import models as m
from . import ocr
from . import security as sec
from . import serializers as ser
from .audit import AuditMiddleware
from .bootstrap import bootstrap
from .database import SessionLocal, engine, get_db
from .migrate import current_revision, upgrade_to_head
from .routers import (
    admin,
    ai as ai_router,
    attachments,
    auth,
    backup,
    inbox,
    invoices,
    master,
    jev_config,
    print_qr,
    reimbursements,
    scan,
    stats,
    upgrade,
)
from .seed import ensure_seed

APP_NAME = "销售费用报销管理工作台"
APP_VERSION = "2.9.17"

FRONTEND_DIR = Path(os.getenv("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend"))

# 允许的跨域来源。默认空 = 不放行任何跨域（前后端同容器，本来就不需要）。
# 生产如果单独部署前端，用逗号分隔写进 CORS_ORIGINS。
CORS_ORIGINS = [o.strip() for o in (os.getenv("CORS_ORIGINS") or "").split(",") if o.strip()]

# 后台自动收票的间隔（分钟）。0 或未配置 = 关闭，只留手动「立即收票」，
# 免得部署在别人机器上默认就去连邮箱。
MAIL_SYNC_MINUTES = int(os.getenv("MAIL_SYNC_INTERVAL_MINUTES", "0") or 0)

BOOT_TIME = time.time()


def _auto_sync_mail() -> None:
    """后台收票线程体：自己开 session，异常只记日志不影响主进程。"""
    from .routers import inbox as inbox_router

    db = SessionLocal()
    try:
        accounts = db.query(m.MailAccount).filter(m.MailAccount.active.is_(True)).all()
        for acc in accounts:
            cfg = inbox_router._account_cfg(acc)
            if not cfg["password"]:
                continue
            try:
                fetched = inbox_router.mailbox.fetch(cfg, limit=acc.max_per_sync)
                summary = inbox_router._import_messages(db, None, acc, fetched["messages"])
                acc.last_sync_at = __import__("datetime").datetime.now()
                acc.imported_total = (acc.imported_total or 0) + summary["imported"]
                acc.last_sync_status = m.IMPORT_OK if not summary["failed"] else m.IMPORT_PARTIAL
                acc.last_sync_detail = (
                    f"[自动] 入库 {summary['imported']} 张，跳过 {summary['skipped']}，失败 {summary['failed']}"
                )
                db.commit()
                if summary["imported"]:
                    print(f"[mail] {acc.name}: 自动收票入库 {summary['imported']} 张")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                acc.last_sync_at = __import__("datetime").datetime.now()
                acc.last_sync_status = m.IMPORT_FAILED
                acc.last_sync_detail = f"[自动] {exc}"[:1000]
                db.commit()
                print(f"[mail] {acc.name} 自动收票失败：{exc}")
    finally:
        db.close()


async def _mail_loop() -> None:
    while True:
        await asyncio.sleep(MAIL_SYNC_MINUTES * 60)
        try:
            await asyncio.to_thread(_auto_sync_mail)
        except Exception as exc:  # noqa: BLE001
            print(f"[mail] 自动收票任务异常：{exc!r}")


# ---------- 在线升级：定时版本检查 ----------

def _auto_check_upgrade() -> None:
    """查一次新版本，把结果写进 setting，供前端「系统更新」卡片读取。"""
    from . import upgrade_client as uc
    from .routers import upgrade as upgrade_router
    db = SessionLocal()
    try:
        rel = uc.fetch_release(db, timeout=25)
        upgrade_router._write_cached(db, upgrade_router._trim(rel))
        if rel.get("ok"):
            flag = "有新版本" if rel.get("has_update") else "已是最新"
            print(f"[upgrade] 版本检查：{flag} {rel.get('version')}（当前 {uc.current_version()}）")
        else:
            print(f"[upgrade] 版本检查失败：{rel.get('error')}")
    finally:
        db.close()


async def _upgrade_loop() -> None:
    """每 N 小时查一次；间隔改了从下一个周期开始生效。"""
    from . import upgrade_client as uc
    await asyncio.sleep(120)      # 别和启动时的迁移/初始化抢资源
    while True:
        try:
            db = SessionLocal()
            try:
                enabled = uc.auto_check_on(db)
                hours = uc.interval_hours(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            enabled, hours = False, 6.0
        if enabled:
            try:
                await asyncio.to_thread(_auto_check_upgrade)
            except Exception as exc:  # noqa: BLE001
                print(f"[upgrade] 自动检查异常：{exc!r}")
        await asyncio.sleep(max(hours, 0.25) * 3600)


# ---------- 数据备份：周期备份循环（v2.9.13+） ----------

def _auto_backup() -> None:
    """跑一次 do_backup + 裁剪，异常只记日志，绝不阻塞主进程。"""
    from . import backup as bk_mod
    db = SessionLocal()
    try:
        result = bk_mod.do_backup(db, label="auto")
        # 跑完顺手裁剪两端，避免长期运行把盘塞满
        bk_mod.prune(db)
        print(
            f"[backup] 自动备份完成：{result.name}  "
            f"大小={result.size // 1024} KB  "
            f"表行数={result.table_rows}  "
            f"上传文件={result.uploaded_files}  "
            f"耗时={result.elapsed_seconds:.1f}s"
        )
        if result.remote_error:
            print(f"[backup] 复制到外挂目录失败（本地包仍可用）：{result.remote_error}")
    except Exception as exc:  # noqa: BLE001
        # 失败也要落 setting，让前端能看到上次失败原因
        try:
            db.rollback()
            row = db.get(m.Setting, "backup_last_result")
            payload = json.dumps({
                "ok": False, "error": f"{type(exc).__name__}: {exc}",
                "finished_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False)
            if row:
                row.value = payload
            else:
                db.add(m.Setting(key="backup_last_result", value=payload,
                                 remark="最近一次备份结果（成功/失败/大小/sha）"))
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        print(f"[backup] 自动备份失败：{exc!r}")
    finally:
        db.close()


async def _backup_loop() -> None:
    """每隔 backup_interval_hours 小时跑一次备份。最短 15 分钟，避免大库连跑。"""
    import json
    while True:
        # 每次循环开头读最新配置（运营可在线改）
        try:
            db = SessionLocal()
            try:
                _, _, _, _, hours, _ = __import__("app.backup", fromlist=["_resolved_paths"])._resolved_paths(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            hours = 24.0
        interval = max(float(hours or 24.0), 0.25)  # 至少 15 分钟
        await asyncio.sleep(interval * 3600)
        try:
            await asyncio.to_thread(_auto_backup)
        except Exception as exc:  # noqa: BLE001
            print(f"[backup] 周期任务异常：{exc!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) 结构升级：失败必须让容器起不来，否则会带着错误结构对外服务
    upgrade_to_head()

    # 2) 数据初始化：补齐参数 + 建初始管理员，失败不阻塞（可能只是权限或并发问题）
    info = bootstrap()
    if info.get("settings_added"):
        print(f"[init] 补充了 {info['settings_added']} 项默认参数")

    # 3) 演示数据：只有 SEED_DEMO=1 才会走到这里，生产默认关闭
    created = ensure_seed()
    if created:
        print(f"[seed] 已生成演示数据：{created}")

    # 4) 可选的定时收票。默认关闭，用 MAIL_SYNC_INTERVAL_MINUTES 打开
    task = None
    if MAIL_SYNC_MINUTES > 0:
        task = asyncio.create_task(_mail_loop())
        print(f"[boot] 邮箱自动收票已开启：每 {MAIL_SYNC_MINUTES} 分钟一次")

    # 5) 在线升级：清掉上一轮遗留的执行器容器，按配置决定是否定时查新版
    upgrade_task = None
    try:
        from . import upgrade_runner as _ur
        _ur.cleanup_worker()
    except Exception as exc:  # noqa: BLE001
        print(f"[upgrade] 清理升级执行器容器失败（忽略）：{exc!r}")
    try:
        from . import upgrade_client as _uc
        _db = SessionLocal()
        try:
            _auto = _uc.auto_check_on(_db)
        finally:
            _db.close()
        if _auto:
            upgrade_task = asyncio.create_task(_upgrade_loop())
            print("[boot] 在线升级自动检查已开启")
    except Exception as exc:  # noqa: BLE001
        print(f"[upgrade] 初始化在线升级检查失败（忽略）：{exc!r}")

    # 6) 数据备份（v2.9.13+）：可选启动期立即一次 + 按配置周期备份
    backup_task = None
    try:
        from . import backup as _bk
        _db = SessionLocal()
        try:
            _bd, _rd, _kl, _kr, _hours, _at_boot = _bk._resolved_paths(_db)
            _bd.mkdir(parents=True, exist_ok=True)
            print(
                f"[boot] 数据备份：周期={_hours}h 本地={_bd} "
                f"外挂={_rd or '(未启用)'} 保留={_kl}/{_kr} 启动即备份={_at_boot}"
            )
        finally:
            _db.close()
        if _at_boot:
            # 启动期一次性：放后台线程跑，不阻塞启动；失败只记日志
            asyncio.get_event_loop().run_in_executor(None, _auto_backup) if False else None
            import threading
            threading.Thread(target=_auto_backup, daemon=True, name="backup-boot").start()
            print("[boot] 数据备份：启动期一次性备份已在后台启动")
        if float(_hours) > 0:
            backup_task = asyncio.create_task(_backup_loop())
            print(f"[boot] 数据备份：周期任务已开启（每 {_hours} 小时一次）")
    except Exception as exc:  # noqa: BLE001
        print(f"[backup] 初始化周期备份失败（忽略）：{exc!r}")

    print(f"[boot] 静态资源目录：{FRONTEND_DIR} (exists={FRONTEND_DIR.exists()})")
    print(f"[boot] 跨域白名单：{CORS_ORIGINS or '（同源，未开启）'}")
    print(f"[boot] 数据库版本：{current_revision()}")
    yield
    for _t in (task, upgrade_task, backup_task):
        if _t:
            _t.cancel()
            with suppress(asyncio.CancelledError):
                await _t


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    lifespan=lifespan,
    # 生产关掉交互式文档，避免把全部接口结构公开给未鉴权访问者。
    # 需要时把 DOCS_ENABLED 设为 1。
    docs_url="/docs" if os.getenv("DOCS_ENABLED", "0") == "1" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if os.getenv("DOCS_ENABLED", "0") == "1" else None,
)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

app.add_middleware(AuditMiddleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底异常：日志留全，响应只给一句话。"""
    import traceback

    print(f"[error] {request.method} {request.url.path} -> {exc!r}")
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请联系管理员"})


app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(master.router)
app.include_router(reimbursements.router)
app.include_router(invoices.router)
app.include_router(attachments.router)
app.include_router(inbox.router)
app.include_router(ai_router.router)
app.include_router(stats.router)
app.include_router(print_qr.router)
app.include_router(jev_config.router)
app.include_router(scan.router)
app.include_router(upgrade.router)
app.include_router(backup.router)


@app.get("/api/health", tags=["系统"])
def health(db: Session = Depends(get_db)):
    """探活接口。刻意不返回数据库连接串——那等于把库地址与账号结构告诉任何人。"""
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "app": APP_NAME,
        "version": APP_VERSION,
        "database": "up" if db_ok else "down",
        "schema_revision": current_revision(),
        "uptime_seconds": int(time.time() - BOOT_TIME),
        "server_date": date.today().isoformat(),
        # 本地 OCR 是否就绪。镜像里 INSTALL_OCR=0 时这里是 none，
        # 运维一条 curl 就能确认「图片型发票到底会不会识别」
        "ocr_engine": ocr.engine_name(),
        # Data Matrix（打印条码）是否就绪。v2.9.3 起 libdmtx 缺失不再拖垮整个服务，
        # 而是在这里如实标出来，配合 /api/print/dm 的 503 一起定位
        "datamatrix": "ready" if print_qr.dmtx_ready() else "unavailable",
    }


def _csv_response(rows: list[list], header: list[str], filename: str, ascii_name: str = "export.csv") -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    # BOM 让 Excel 正确识别 UTF-8 中文
    body = "\ufeff" + buf.getvalue()
    # HTTP 头只能是 latin-1，中文文件名必须走 RFC 5987 的 filename* 参数
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=body.encode("utf-8"),
        headers={"Content-Disposition": disposition},
        media_type="text/csv; charset=utf-8",
    )


@app.get("/api/export/reimbursements.csv", tags=["导出"])
def export_reimbursements(
    status: str | None = None,
    department_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    q = db.query(m.Reimbursement).filter(*sec.scope_conds(user))
    if status:
        q = q.filter(m.Reimbursement.status.in_(status.split(",")))
    if department_id:
        q = q.filter(m.Reimbursement.department_id == department_id)
    if date_from:
        q = q.filter(m.Reimbursement.occur_start >= date_from)
    if date_to:
        q = q.filter(m.Reimbursement.occur_end <= date_to)

    header = ["单号", "标题", "申请人", "部门", "客户", "项目", "事由", "状态",
              "金额", "费用起", "费用止", "提交时间", "审批时间", "付款时间", "审批人", "驳回原因"]
    rows = []
    for r in q.order_by(m.Reimbursement.id.desc()).all():
        d = ser.reimbursement_out(r)
        rows.append([
            d["code"], d["title"], d["applicant_name"] or "", d["department_name"] or "",
            d["customer_name"] or "", d["project_name"] or "", d["purpose"] or "", d["status"],
            d["total_amount"], d["occur_start"] or "", d["occur_end"] or "",
            d["submit_at"] or "", d["approve_at"] or "", d["pay_at"] or "",
            d["approver"] or "", d["reject_reason"] or "",
        ])
    return _csv_response(rows, header, "报销单台账.csv", "reimbursements.csv")


@app.get("/api/export/invoices.csv", tags=["导出"])
def export_invoices(
    check_status: str | None = None,
    unlinked: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    q = db.query(m.Invoice).filter(*sec.invoice_scope_conds(user))
    if check_status:
        q = q.filter(m.Invoice.check_status.in_(check_status.split(",")))
    if unlinked:
        q = q.filter(m.Invoice.reimbursement_id.is_(None))
    if date_from:
        q = q.filter(m.Invoice.invoice_date >= date_from)
    if date_to:
        q = q.filter(m.Invoice.invoice_date <= date_to)

    header = ["发票号码", "发票代码", "发票类型", "价税合计", "税率", "税额", "开票日期",
              "销售方", "购买方", "查验状态", "查验结果", "费用类型", "关联单号", "备注"]
    rows = []
    for v in q.order_by(m.Invoice.id.desc()).all():
        d = ser.invoice_out(v)
        rows.append([
            d["invoice_no"], d["invoice_code"] or "", d["invoice_type"], d["amount"],
            d["tax_rate"], d["tax_amount"], d["invoice_date"] or "", d["seller_name"] or "",
            d["buyer_name"] or "", d["check_status"], d["check_result"] or "",
            d["category_name"] or "", d["reimbursement_code"] or "", d["remark"] or "",
        ])
    return _csv_response(rows, header, "发票台账.csv", "invoices.csv")


@app.get("/api/export/items.csv", tags=["导出"])
def export_items(
    date_from: str | None = None,
    date_to: str | None = None,
    user: m.AppUser = Depends(sec.current_user),
    db: Session = Depends(get_db),
):
    q = (
        db.query(m.ReimbursementItem)
        .join(m.Reimbursement, m.ReimbursementItem.reimbursement_id == m.Reimbursement.id)
        .filter(*sec.scope_conds(user))
    )
    if date_from:
        q = q.filter(m.ReimbursementItem.occur_date >= date_from)
    if date_to:
        q = q.filter(m.ReimbursementItem.occur_date <= date_to)

    header = ["单号", "费用发生日", "费用类型", "费用大类", "金额", "税额", "摘要",
              "发票张数", "申请人", "部门", "客户", "状态"]
    rows = []
    for it in q.order_by(m.ReimbursementItem.id.desc()).all():
        r = it.reimbursement
        rows.append([
            r.code if r else "", it.occur_date.isoformat() if it.occur_date else "",
            it.category.name if it.category else "", it.category.group_name if it.category else "",
            ser.money(it.amount), ser.money(it.tax_amount), it.description or "",
            len(it.invoices), r.applicant.name if r and r.applicant else "",
            r.department.name if r and r.department else "",
            r.customer.name if r and r.customer else "", r.status if r else "",
        ])
    return _csv_response(rows, header, "费用明细台账.csv", "expense_items.csv")


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
