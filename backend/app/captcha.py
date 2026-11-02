"""登录图形验证码（v2.9.38）—— 零依赖 SVG 生成 + 无状态签名校验。

两条刻意的技术选择：

1. **不用 Pillow**：本项目坚持零编译依赖（老 Docker 引擎上装包会翻车），
   而验证码只是几个字符加几条干扰线，SVG 天生够用，还自带矢量清晰、
   不必为不同 DPI 出多套图。

2. **服务端不存答案**：容器可能是多 worker，内存字典不共享；存库又得多一张
   表和定时清理任务。这里把「答案 + 过期时间」用 HMAC 签名后直接交给前端
   （key 就是签名串），校验时重算签名即可，进程间天然一致，也不需要任何存储。
   代价是无法强制「一次一用」——所以用内存集合记下已用过的签名做尽力而为的
   兜底：多 worker 下退化成「签名在 3 分钟内可重放一次」，对一个只用来挡
   撞库的验证码来说，这个风险可以接受。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import urllib.parse

from . import crypt

# 验证码有效期（秒）。太短用户来不及输，太长等于给撞库留窗口
TTL_SEC = 180

# 去掉 0/O、1/I/L 这类肉眼分不清的字符——验证码挡的是脚本，不是人
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
LEN = 4

_W = 112
_H = 40

# 已用过的签名（best-effort 防重放，见模块注释）
_USED: dict[str, float] = {}
_USED_CAP = 4096


def _secret() -> bytes:
    # 复用邮箱凭据那把主密钥，不另开一个 env 让运维多记一件事
    return hashlib.sha256(b"wb-reimburse/captcha/v1/" + crypt.get_secret().encode("utf-8")).digest()


def _prune() -> None:
    """按时间裁掉过期记录，别让字典无限长（验证码接口是匿名的）。"""
    if len(_USED) <= _USED_CAP:
        return
    now = time.time()
    for k, ts in list(_USED.items()):
        if now - ts > TTL_SEC:
            _USED.pop(k, None)


def issue() -> tuple[str, str]:
    """生成一组验证码，返回 (明文答案, 交给前端的 key)。"""
    code = "".join(secrets.choice(ALPHABET) for _ in range(LEN))
    exp = int(time.time()) + TTL_SEC
    payload = f"{code}.{exp}"
    mac = hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()[:24]
    return code, f"{payload}.{mac}"


def verify(key: str, answer: str) -> bool:
    """校验用户填的验证码。大小写不敏感（验证码的字形本来就有大写小写之争）。"""
    if not key or not answer:
        return False
    try:
        code, exp, mac = str(key).split(".")
        expect = hmac.new(_secret(), f"{code}.{exp}".encode("ascii"), hashlib.sha256).hexdigest()[:24]
    except (ValueError, AttributeError):
        return False
    if not hmac.compare_digest(mac, expect):
        return False
    if int(exp) < time.time():
        return False
    if (answer or "").strip().upper() != code.upper():
        return False
    # 用过即废：同一张图不能拿来反复试密码
    _prune()
    if mac in _USED:
        return False
    _USED[mac] = time.time()
    return True


def svg(code: str) -> str:
    """把答案画成 SVG。字符逐字做位移 / 旋转 / 深浅变化，加干扰线与噪点。"""
    rnd = secrets.SystemRandom()
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" '
        f'viewBox="0 0 {_W} {_H}" role="img" aria-label="图形验证码">',
        f'<rect width="{_W}" height="{_H}" fill="#f4f7fb"/>',
    ]
    # 干扰线：压在字符下层，颜色刻意压得很淡，保证不盖住可读性
    for _ in range(4):
        x1, y1 = rnd.uniform(2, _W - 2), rnd.uniform(2, _H - 2)
        x2, y2 = rnd.uniform(2, _W - 2), rnd.uniform(2, _H - 2)
        w = round(rnd.uniform(0.6, 1.4), 2)
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="#c3d0e0" stroke-width="{w}"/>'
        )
    # 噪点
    for _ in range(26):
        x, y = rnd.uniform(1, _W - 1), rnd.uniform(1, _H - 1)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{round(rnd.uniform(0.5, 1.2), 2)}" fill="#b8c6d8"/>')

    # 字符：等宽字体 + 逐字旋转，深浅在深蓝到深灰之间取
    inks = ("#1a3c6e", "#264a6b", "#33475b", "#1f4f6e")
    step = _W / (len(code) + 0.6)
    for i, ch in enumerate(code):
        x = step * (i + 0.55)
        y = 27 + rnd.uniform(-2.5, 2.5)
        rot = rnd.uniform(-13, 13)
        size = round(rnd.uniform(21, 25), 1)
        fill = rnd.choice(inks)
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="700" '
            f'fill="{fill}" font-family="Menlo,Consolas,Monaco,monospace" '
            f'text-anchor="middle" transform="rotate({rot:.1f} {x:.1f} {y:.1f})">{ch}</text>'
        )
    parts.append(f'<rect x="0.5" y="0.5" width="{_W - 1}" height="{_H - 1}" fill="none" stroke="#d5dde8"/>')
    parts.append("</svg>")
    return "".join(parts)


def data_uri(code: str) -> str:
    """SVG → 可直接塞进 <img src> 的 data URI。

    用 percent-encode 而不是 base64：浏览器同样认，字符串短一半，
    而且运维在 F12 里能直接读懂这段 SVG（排查"验证码不显示"时很有用）。
    """
    return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg(code), safe="")
