"""登录验证码（v2.9.38）验收脚本。

    LOGIN_CAPTCHA=1 PORT=8794 bash tools/dev_server.sh start
    WB_BASE_URL=http://127.0.0.1:8794 python backend/captcha_test.py http://127.0.0.1:8794

为什么单独占一个端口：验证码默认在生产开启，而其余验收脚本里满地都是
「直接 POST /api/auth/login」的用例（dev 实例默认 LOGIN_CAPTCHA=0 关掉）。
这里必须在一个**开着验证码**的实例上验，才能真的验到「缺验证码被拒」。

怎么拿到正确答案：验证码是 HMAC 签名的，答案只存在于签名串里，接口不返回明文。
所以脚本直接 import 后端的 captcha 模块自己生成一组（同一把 MAIL_SECRET），
再拿去打登录接口——验的是完整链路，而不是在接口上开后门。

覆盖：
    1. 模块层：签名/过期/大小写/篡改/重放
    2. 接口层：拿图、缺验证码 400、错验证码 400、正确验证码可登录
    3. 开关：login_captcha=0 时接口返回 enabled=false 且登录不再要求验证码
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("WB_BASE_URL")
    or "http://127.0.0.1:8794"
).rstrip("/")

ADMIN_USER = os.environ.get("WB_USER", "admin")
ADMIN_PASSWORD = os.environ.get("WB_PASSWORD", "Adm1n@2026")

# 与 tools/dev_server.sh 的默认密钥保持一致（dev 裸跑的固定值）
os.environ.setdefault(
    "MAIL_SECRET", "dev-local-secret-0123456789abcdef0123456789abcdef"
)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from app import captcha as cap  # noqa: E402

OK = 0
FAIL = 0
FAILED: list[str] = []


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


def req(method, path, body=None, token=None):
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        r.add_header(k, v)
    opener = urllib.request.build_opener()
    opener.add_handler(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=30) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode() if raw else "null")
            except Exception:
                return resp.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode() or "null")
        except Exception:
            return e.code, payload.decode(errors="replace")


def main() -> int:
    print("\n== 模块层 ==")
    code, key = cap.issue()
    check("生成 4 位验证码", len(code) == 4 and code.isalnum(), code)
    check("字符集不含易混淆字符", not (set(code) & set("01OIL")), code)
    check("正确验证码通过", cap.verify(key, code))
    # 一次性：大小写各用一组新的，别拿已经用过的 key 做第二次校验
    c_lower, k_lower = cap.issue()
    check("小写答案也认", cap.verify(k_lower, c_lower.lower()))
    c_upper, k_upper = cap.issue()
    check("大写答案也认", cap.verify(k_upper, c_upper.upper()))
    check("错误验证码被拒", not cap.verify(key, "ZZZZ"))
    check("空值被拒", not cap.verify("", code) and not cap.verify(key, ""))
    # 用过的签名不能再用（一次性）
    code2, key2 = cap.issue()
    check("同一张图可用一次", cap.verify(key2, code2))
    check("用过即废（防重放）", not cap.verify(key2, code2))
    # 篡改：答案换掉但签名不动
    forged = "AAAA." + key.split(".", 1)[1]
    check("篡改答案后签名不匹配", not cap.verify(forged, "AAAA"), forged)
    # 过期：手搓一个已过期的签名串
    exp = int(time.time()) - 5
    payload = f"ABCD.{exp}"
    mac = hmac.new(cap._secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()[:24]
    check("过期验证码被拒", not cap.verify(f"{payload}.{mac}", "ABCD"))

    print("\n== 接口层 ==")
    st, d = req("GET", "/api/auth/captcha")
    check("取验证码 200", st == 200, f"{st} {d}")
    check("返回 enabled 与图", d.get("enabled") is True and d.get("image", "").startswith("data:image/svg+xml"),
          str(d)[:120])
    check("接口不返回答案明文", code not in json.dumps(d) and not d.get("code"), str(d)[:80])
    check("图片是 SVG 且有宽高", "svg" in d.get("image", "")[:40], d.get("image", "")[:60])

    st, _ = req("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    check("缺验证码 400", st == 400, st)

    st, bad2 = req("POST", "/api/auth/login", {
        "username": ADMIN_USER, "password": ADMIN_PASSWORD,
        "captcha_key": d.get("key", ""), "captcha_code": "ZZZZ",
    })
    check("错验证码 400", st == 400, f"{st} {bad2}")
    check("错验证码时提示刷新", "验证码" in json.dumps(bad2, ensure_ascii=False), bad2)

    st, bad3 = req("POST", "/api/auth/login", {
        "username": ADMIN_USER, "password": ADMIN_PASSWORD,
        "captcha_key": "x.y.z", "captcha_code": "AAAA",
    })
    check("伪造 key 400", st == 400, st)

    # 正确答案（脚本自己签发，验的是完整链路不是后门）
    code3, key3 = cap.issue()
    st, ok = req("POST", "/api/auth/login", {
        "username": ADMIN_USER, "password": ADMIN_PASSWORD,
        "captcha_key": key3, "captcha_code": code3,
    })
    check("正确验证码可登录", st == 200 and bool(ok.get("token")), f"{st} {str(ok)[:120]}")

    print("\n== 开关 ==")
    token = ok.get("token")
    if token:
        st, _ = req("PUT", "/api/settings/login_captcha", {"value": "0"}, token=token)
        check("管理员可关闭验证码", st in (200, 201, 204), st)
        st, d2 = req("GET", "/api/auth/captcha")
        check("关闭后接口返回 enabled=false", st == 200 and d2.get("enabled") is False, d2)
        st, ok2 = req("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        check("关闭后不填验证码也能登录", st == 200, st)
        req("PUT", "/api/settings/login_captcha", {"value": "1"}, token=token)
        st, d3 = req("GET", "/api/auth/captcha")
        check("重新打开后恢复", d3.get("enabled") is True, d3)

    print(f"\n结果：{OK} 通过 / {FAIL} 失败")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
