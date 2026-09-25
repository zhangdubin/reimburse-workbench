"""IMAP 收票通道（标准库 imaplib / email，零外部依赖）。

职责边界：本模块只负责「把邮件取回来并拆成干净的数据结构」，
不碰数据库，也不决定哪些附件该入库——那是 routers/inbox.py 的事。

拿回来的每封邮件形如：
    {
      "uid": "1234", "message_id": "<...@...>", "subject": "...", "sender": "...",
      "sent_at": "2026-08-14 10:20:31", "text": "主题... 内容...",
      "attachments": [{"filename": "xx.pdf", "data": b"...", "mime": "application/pdf"}],
    }
"""

from __future__ import annotations

import email
import imaplib
import re
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from . import filestore as fs

TIMEOUT = 30
MAX_MAIL_BYTES = 25 * 1024 * 1024  # 单封邮件的抓取上限，防止超大附件拖垮进程
MAX_ATTACH_PER_MAIL = 20

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailboxError(RuntimeError):
    """连接、鉴权或协议层面的失败，消息可直接展示给用户。"""


def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        return str(make_header(decode_header(str(raw)))).strip()
    except Exception:  # noqa: BLE001
        return str(raw).strip()


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _imap_date(d: date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def connect(cfg: dict, *, folder: str | None = None):
    """建立并返回已 select 的 IMAP 连接。caller 负责 logout。"""
    host = (cfg.get("host") or "").strip()
    if not host:
        raise MailboxError("邮箱服务器地址未配置")
    port = int(cfg.get("port") or (993 if cfg.get("use_ssl", True) else 143))
    try:
        if cfg.get("use_ssl", True):
            conn = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT)
        else:
            conn = imaplib.IMAP4(host, port, timeout=TIMEOUT)
    except OSError as exc:
        raise MailboxError(f"无法连接 {host}:{port} —— {exc}") from exc
    try:
        conn.login(cfg.get("username") or "", cfg.get("password") or "")
    except imaplib.IMAP4.error as exc:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
        raise MailboxError(f"登录失败，请检查账号或授权码：{_decode(exc)}") from exc
    target = folder or cfg.get("folder") or "INBOX"
    typ, data = conn.select(target, readonly=False)
    if typ != "OK":
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
        raise MailboxError(f"无法打开文件夹 {target}：{_decode(data)}")
    return conn


def test_connection(cfg: dict) -> dict:
    """测试连接：成功时顺带把文件夹列表带回来，方便前端勾选。"""
    conn = connect(cfg)
    try:
        folders: list[str] = []
        try:
            typ, data = conn.list()
            if typ == "OK":
                for line in data or []:
                    text = _decode(line)
                    m = re.search(r'"([^"]+)"\s*$', text)
                    if m:
                        folders.append(m.group(1))
        except imaplib.IMAP4.error:
            pass
        return {"ok": True, "folders": folders[:200], "message": "连接成功"}
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def _extract_body(msg: Message) -> str:
    """取正文：优先纯文本，没有就用 HTML 去标签。只取第一段够用的。"""
    plain: list[str] = []
    html: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                text = part.get_content()
            except Exception:  # noqa: BLE001
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
            (plain if ctype == "text/plain" else html).append(str(text))
    else:
        try:
            text = msg.get_content()
        except Exception:  # noqa: BLE001
            text = (msg.get_payload(decode=True) or b"").decode("utf-8", errors="ignore")
        (plain if msg.get_content_type() == "text/plain" else html).append(str(text))

    if plain:
        return re.sub(r"\s+", " ", " ".join(plain)).strip()[:20000]
    return _strip_html(" ".join(html))[:20000]


def _extract_attachments(msg: Message) -> list[dict]:
    """拆出附件。内嵌图片（有 Content-ID 的）跳过，那通常是签名里的 logo。"""
    out: list[dict] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        if not filename and disposition != "attachment":
            continue
        if part.get("Content-ID") and disposition != "attachment":
            continue
        name = _decode(filename) or "attachment"
        ext = fs.safe_ext(name)
        if ext not in fs.ALLOWED:
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload or len(payload) > fs.MAX_BYTES:
            continue
        out.append({
            "filename": name[:255],
            "data": payload,
            "mime": part.get_content_type(),
        })
        if len(out) >= MAX_ATTACH_PER_MAIL:
            break
    return out


def _match_filters(subject: str, sender: str, cfg: dict) -> tuple[bool, str]:
    """按配置过滤邮件，返回 (是否收, 原因)。"""
    keywords = [k.strip() for k in (cfg.get("subject_keywords") or "").replace("，", ",").split(",") if k.strip()]
    if keywords and not any(k.lower() in subject.lower() for k in keywords):
        return False, f"主题不含关键词 {keywords}"

    allow = [s.strip() for s in (cfg.get("sender_allow") or "").replace("，", ",").split(",") if s.strip()]
    if allow:
        low = sender.lower()
        hit = any(
            (re.fullmatch(re.escape(a).replace(r"\*", ".*"), low, re.I) is not None) if "*" in a else a.lower() in low
            for a in allow
        )
        if not hit:
            return False, "发件人不在白名单"
    return True, ""


def fetch(cfg: dict, *, limit: int = 30) -> dict:
    """抓一批邮件。返回 {messages: [...], scanned: n, skipped: n}。

    检索条件：既配了 since_days 又开了 only_unseen 时用「未读 + 时间窗」，
    两者取交集，避免第一次接入就把整个收件箱拉一遍。
    """
    limit = max(1, min(int(limit or 30), 200))
    conn = connect(cfg)
    try:
        only_unseen = bool(cfg.get("only_unseen", True))
        days = cfg.get("since_days")
        criteria = []
        if only_unseen:
            criteria.append("UNSEEN")
        if days:
            since = date.today() - timedelta(days=int(days))
            criteria.append(f'SINCE {_imap_date(since)}')
        query = "(" + " ".join(criteria) + ")" if criteria else "ALL"

        typ, data = conn.uid("SEARCH", None, query)
        if typ != "OK":
            raise MailboxError(f"检索邮件失败：{_decode(data)}")
        uids = (data[0] or b"").split()
        total = len(uids)
        # 从最新的开始收，避免老邮件把配额占满
        picked = uids[-limit:]

        messages: list[dict] = []
        skipped = 0
        for uid in reversed(picked):
            try:
                typ, raw = conn.uid("FETCH", uid, "(RFC822)")
            except imaplib.IMAP4.error as exc:
                skipped += 1
                continue
            if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                skipped += 1
                continue
            blob = raw[0][1]
            if not blob or len(blob) > MAX_MAIL_BYTES:
                skipped += 1
                continue
            try:
                msg = email.message_from_bytes(blob)
            except Exception:  # noqa: BLE001
                skipped += 1
                continue

            subject = _decode(msg.get("Subject"))
            sender = _decode(msg.get("From"))
            ok, _why = _match_filters(subject, sender, cfg)
            if not ok:
                skipped += 1
                continue
            try:
                sent = parsedate_to_datetime(msg.get("Date") or "")
                sent_at = sent.astimezone().strftime("%Y-%m-%d %H:%M:%S") if sent else None
            except Exception:  # noqa: BLE001
                sent_at = None

            body = _extract_body(msg)
            attachments = _extract_attachments(msg)
            messages.append({
                "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                "message_id": (msg.get("Message-ID") or "").strip()[:255],
                "subject": subject[:255],
                "sender": sender[:255],
                "sent_at": sent_at,
                "text": f"主题 {subject} 内容 {body}" if (subject or body) else "",
                "attachments": attachments,
                "mark_seen": bool(cfg.get("mark_seen", True)),
            })

        # 标记已读放在最后批量做，中途出错也不会把没收的邮件标成已读
        if cfg.get("mark_seen", True):
            for item in messages:
                try:
                    conn.uid("STORE", item["uid"], "+FLAGS", "(\\Seen)")
                except imaplib.IMAP4.error:
                    pass
        return {"messages": messages, "scanned": total, "skipped": skipped}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
