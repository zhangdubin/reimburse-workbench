"""发票影像 / 电子发票原件的落盘与清理。

单独抽出来是因为现在有两条写入路径（人工上传、邮箱收票），
白名单、大小上限、路径穿越防御必须是同一份实现，否则一定会漂移。
"""

from __future__ import annotations

import mimetypes
import os
import secrets
from pathlib import Path

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "uploads"
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(_DEFAULT_DIR)))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "10"))
MAX_BYTES = MAX_MB * 1024 * 1024

# 后缀 -> 允许的 MIME（双向白名单）。电子发票的结构化格式（ofd/xml/xlsx）也放行，
# 它们既是「影像」也是「原件」，识别引擎要靠它们抽字段。
ALLOWED: dict[str, set[str]] = {
    ".pdf": {"application/pdf"},
    ".ofd": {"application/ofd", "application/octet-stream", "application/zip"},
    ".xml": {"text/xml", "application/xml", "text/plain", "application/octet-stream"},
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
    },
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
    ".webp": {"image/webp"},
}

# 浏览器能内联预览的类型
INLINE_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}

EXT_LABEL = "/".join(sorted(ALLOWED))


class FileRejected(ValueError):
    """文件不合格（后缀、类型或体积）。"""


def safe_ext(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def save_bytes(filename: str, data: bytes, mime: str | None = None, *, check_ext: bool = True) -> dict:
    """把字节落盘，返回可直接喂给 InvoiceAttachment 的字段。

    磁盘名一律自己生成（随机 hex + 白名单后缀），绝不使用调用方给的文件名，
    这样 `../../etc/passwd` 这类路径穿越在入口就被掐断。
    """
    raw_name = (filename or "").strip() or "attachment"
    ext = safe_ext(raw_name)
    if check_ext and ext not in ALLOWED:
        raise FileRejected(f"只允许 {'/'.join(sorted(ALLOWED))} 格式，收到 {ext or '无后缀'}")
    if not data:
        raise FileRejected("文件内容为空")
    if len(data) > MAX_BYTES:
        raise FileRejected(f"文件超过 {MAX_MB}MB 限制")

    stored = f"{secrets.token_hex(16)}{ext}"
    (UPLOAD_DIR / stored).write_bytes(data)

    real_mime = mime
    if not real_mime or real_mime == "application/octet-stream":
        real_mime = mimetypes.guess_type(raw_name)[0] or "application/octet-stream"
    return {
        "filename": Path(raw_name).name[:255] or stored,
        "stored_name": stored,
        "size": len(data),
        "mime": real_mime,
    }


def resolve(stored_name: str) -> Path:
    """把磁盘名还原成绝对路径，并确保没跑出 UPLOAD_DIR。"""
    path = UPLOAD_DIR / (stored_name or "")
    try:
        if not path.resolve().is_relative_to(UPLOAD_DIR.resolve()):
            raise FileRejected("非法的附件路径")
    except AttributeError:  # Python < 3.9 兜底
        if not str(path.resolve()).startswith(str(UPLOAD_DIR.resolve())):
            raise FileRejected("非法的附件路径")
    return path


def delete_stored(stored_name: str) -> bool:
    """删文件。文件不存在或删不掉都不抛异常——业务记录该删还得删。"""
    if not stored_name:
        return False
    try:
        path = resolve(stored_name)
    except FileRejected:
        return False
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
