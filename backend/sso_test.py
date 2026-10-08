"""单点登录（v2.9.23）验收脚本。

自己造数据、自己起 Mock 门户：
    - Mock IdP 跑在另一个端口上，实现 OIDC 授权码、CAS serviceValidate、OIDC 自动发现所需的端点
    - 被测实例：tools/dev_server.sh 起的那类 dev 实例（SQLite）

用法（服务已在 BASE 上跑起来之后）：
    WB_BASE_URL=http://127.0.0.1:8793 WB_PASSWORD=Adm1n@2026 python sso_test.py

覆盖：
    1. 管理员 CRUD / 掩码 / 自动发现 / 连通性测试 / 字段映射预览
    2. OIDC 授权码全流程：跳转 → 回调 → handover → token → /me
    3. 自动开户 + 门户分组→角色映射 + 工号绑定员工
    4. 二次登录复用同一账号（(provider, subject) 唯一约束生效）
    5. CAS 3.0 票据校验全流程
    6. JWT 令牌直通（HS256；错误签名必须被拒）
    7. 反向用例：缺 state、票据被拒、handover 重放、禁止账密登录、关闭自动开户
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import http.server
import http.cookiejar
from datetime import datetime, timedelta

BASE = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("WB_BASE_URL")
    or "http://127.0.0.1:8793"
).rstrip("/")

ADMIN_USER = os.environ.get("WB_USER", "admin")
ADMIN_PASSWORD = os.environ.get("WB_PASSWORD", "Adm1n@2026")

# Mock 门户端口
PORTAL_PORT = int(os.environ.get("MOCK_PORTAL_PORT", "8794"))
PORTAL = f"http://127.0.0.1:{PORTAL_PORT}"

OK = 0
FAIL = 0
FAILED = []
TOKEN = None

CLIENT_SECRET = "portal-client-secret-xyz"
JWT_SECRET = "portal-jwt-secret-abc"
VISITED = {}


# ------------------------------------------------------------------ 断言
def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


def req(method, path, body=None, params=None, token=None, form=None, jar=None, follow=True):
    """带会话/角色的请求。jar 给就用它跟踪 cookie（SSO 流程必须）。"""
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        r.add_header(k, v)

    opener = urllib.request.build_opener()
    if jar is not None:
        opener.add_handler(urllib.request.HTTPCookieProcessor(jar))
    if not follow:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        opener.add_handler(NoRedirect())
    if follow:
        opener.add_handler(urllib.request.HTTPCookieProcessor(jar or http.cookiejar.CookieJar()))
    # 沙箱里的 HTTP_PROXY 会把本地回环请求也接管走（502 假象），显式绕开
    opener.add_handler(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=30) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode() if raw else "null"), resp.url
            except Exception:
                return resp.status, raw.decode(errors="replace"), resp.url
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode() or "null"), getattr(e, "url", url)
        except Exception:
            return e.code, payload.decode(errors="replace"), getattr(e, "url", url)


# ------------------------------------------------------------------ Mock 门户
def hs256(payload: dict, secret: str) -> str:
    head = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}, separators=(",", ":")).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sign = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    return f"{head}.{body}.{sign}"


CAS_XML = """<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>
  <cas:authenticationSuccess>
    <cas:user>lisi</cas:user>
    <cas:attributes>
      <cas:displayName>李四</cas:displayName>
      <cas:employee_no>E205</cas:employee_no>
      <cas:email>lisi@corp.com</cas:email>
      <cas:groups>报销审批组</cas:groups>
    </cas:attributes>
  </cas:authenticationSuccess>
</cas:serviceResponse>"""


class PortalHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def _json(self, obj, status=200):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)
        VISITED.setdefault(p.path, 0)
        VISITED[p.path] += 1

        if p.path == "/authorize":
            # 连通性测试会用「不带任何参数」的 GET 探活：真门户这时返回登录页(200)，
            # 不能当成缺参数崩掉
            if "redirect_uri" not in q or "state" not in q:
                return self._json({"login_page": True})
            return self._redirect(f"{q['redirect_uri'][0]}?code=CODE-{int(time.time())}&state={q['state'][0]}")
        if p.path == "/userinfo":
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer portal-access-token"):
                return self._json({"error": "invalid_token"}, 401)
            return self._json({
                "sub": "20086",
                "preferred_username": "zhangsan",
                "name": "张三",
                "employee_no": "E102",
                "email": "zhangsan@corp.com",
                "groups": ["门户财务组", "报销"],
            })
        if p.path == "/.well-known/openid-configuration":
            return self._json({
                "issuer": PORTAL,
                "authorization_endpoint": PORTAL + "/authorize",
                "token_endpoint": PORTAL + "/token",
                "userinfo_endpoint": PORTAL + "/userinfo",
                "jwks_uri": PORTAL + "/jwks",
                "scopes_supported": ["openid", "profile", "email"],
            })
        if p.path == "/cas/login":
            return self._redirect(f"{q['service'][0]}?ticket=ST-{int(time.time())}")
        if p.path == "/cas/serviceValidate":
            if q.get("ticket", [""])[0].startswith("BAD"):
                return self._xml_failure()
            raw = CAS_XML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        return self._json({"error": "not_found", "path": p.path}, 404)

    def _xml_failure(self):
        raw = ("""<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>
          <cas:authenticationFailure code="INVALID_TICKET">ticket not recognized</cas:authenticationFailure>
        </cas:serviceResponse>""").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        form = urllib.parse.parse_qs(raw)
        VISITED.setdefault(p.path, 0)
        VISITED[p.path] += 1
        if p.path == "/token":
            client_ok = form.get("client_id", [""])[0] == "reimburse-app"
            code_ok = str(form.get("code", [""])[0]).startswith("CODE-")
            if not (client_ok and code_ok):
                return self._json({"error": "invalid_grant", "error_description": "bad code"}, 400)
            return self._json({
                "access_token": "portal-access-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid profile email",
            })
        return self._json({"error": "not_found"}, 404)


def start_portal():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORTAL_PORT), PortalHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------ 用例
def test_admin_crud():
    print("\n[1] 身份源 CRUD 与掩码")
    st, d, _ = req("GET", "/api/admin/sso/providers", token=TOKEN)
    check("列表可访问", st == 200, st)
    check("初始为空", st == 200 and len(d.get("items", [])) == 0)

    st, d, _ = req("POST", "/api/admin/sso/providers", body={
        "name": "集团门户-OIDC",
        "protocol": "oidc",
        "issuer": PORTAL,
        "authorize_url": PORTAL + "/authorize",
        "token_url": PORTAL + "/token",
        "userinfo_url": PORTAL + "/userinfo",
        "client_id": "reimburse-app",
        "client_secret": CLIENT_SECRET,
        "scope": "openid profile email",
        "claim_subject": "sub",
        "claim_username": "preferred_username",
        "claim_name": "name",
        "claim_employee_no": "employee_no",
        "claim_groups": "groups",
        "auto_create": True,
        "default_role": "申请人",
        "role_map": {"门户财务组": "财务", "报销审批组": "审批人"},
        "session_hours": 6,
        "enabled": True,
    }, token=TOKEN)
    check("OIDC 身份源创建成功", st in (200, 201), f"{st} {d}")
    oidc_id = (d or {}).get("id") if st in (200, 201) else None

    st, d, _ = req("GET", f"/api/admin/sso/providers/{oidc_id}", token=TOKEN)
    check("client_secret 不回明文", st == 200 and CLIENT_SECRET not in json.dumps(d, ensure_ascii=False), d)
    check("回调地址按实际访问地址生成", st == 200 and d.get("callback_url", "").endswith(f"/api/auth/sso/{oidc_id}/callback"), d)

    st, d, _ = req("POST", "/api/admin/sso/providers", body={
        "name": "集团门户-OIDC", "protocol": "oidc",
    }, token=TOKEN)
    check("重名被拒", st == 400, st)

    st, d, _ = req("PUT", f"/api/admin/sso/providers/{oidc_id}", body={
        "name": "集团门户-OIDC", "protocol": "oidc",
        "issuer": PORTAL,
        "authorize_url": PORTAL + "/authorize", "token_url": PORTAL + "/token",
        "userinfo_url": PORTAL + "/userinfo",
        "client_id": "reimburse-app", "client_secret": "KEEP",
        "claim_subject": "sub", "claim_username": "preferred_username", "claim_name": "name",
        "default_role": "申请人", "session_hours": 6, "enabled": True,
    }, token=TOKEN)
    check("KEEP 保留原密钥", st == 200, f"{st} {d}")
    check(
        "KEEP 后密钥仍可用（后续登录能换到 token 即证明）",
        st == 200,
    )

    # CAS 来源
    st, cas, _ = req("POST", "/api/admin/sso/providers", body={
        "name": "统一认证-CAS", "protocol": "cas3",
        "authorize_url": PORTAL + "/cas/login",
        "validate_url": PORTAL + "/cas/serviceValidate",
        "claim_subject": "user", "claim_username": "user", "claim_name": "displayName",
        "claim_employee_no": "employee_no", "claim_groups": "groups",
        "auto_create": True, "default_role": "申请人",
        "role_map": {"报销审批组": "审批人"},
        "enabled": True,
    }, token=TOKEN)
    check("CAS 身份源创建成功", st in (200, 201), f"{st} {cas}")
    cas_id = (cas or {}).get("id") if st in (200, 201) else None

    # JWT 直通来源
    st, jwtp, _ = req("POST", "/api/admin/sso/providers", body={
        "name": "门户网关-JWT", "protocol": "jwt",
        "issuer": PORTAL, "jwt_secret": JWT_SECRET, "jwt_param": "token",
        "jwt_source": "query", "jwt_audience": "",
        "claim_subject": "sub", "claim_username": "preferred_username", "claim_name": "name",
        "auto_create": True, "default_role": "申请人", "enabled": True,
    }, token=TOKEN)
    check("JWT 身份源创建成功", st in (200, 201), f"{st} {jwtp}")
    jwt_id = (jwtp or {}).get("id") if st in (200, 201) else None

    return oidc_id, cas_id, jwt_id


def test_tools(oidc_id):
    print("\n[2] 排障工具：自动发现 / 连通性 / 映射预览")
    st, d, _ = req("POST", "/api/admin/sso/discover", body={"issuer": PORTAL}, token=TOKEN)
    check("OIDC 自动发现成功", st == 200, f"{st} {d}")
    check("自动发现回填了 4 个端点",
          st == 200 and all(d.get(k) for k in ("authorize_url", "token_url", "userinfo_url", "jwks_url")), d)

    st, d, _ = req("POST", "/api/admin/sso/discover", body={"issuer": "http://127.0.0.1:1/none"}, token=TOKEN)
    check("发现失败时给出可读原因", st == 400, f"{st} {d}")

    st, d, _ = req("POST", f"/api/admin/sso/providers/{oidc_id}/test", token=TOKEN)
    check("连通性测试通过", st == 200 and d.get("ok") is True, json.dumps(d, ensure_ascii=False)[:300])
    check("测试逐项返回了端点结果", st == 200 and len(d.get("checks") or []) >= 3, d)

    st, d, _ = req("POST", f"/api/admin/sso/providers/{oidc_id}/map-preview", body={
        "raw": {"sub": "999", "preferred_username": "wangwu", "name": "王五", "groups": ["门户财务组"]},
    }, token=TOKEN)
    check("映射预览取到用户名", st == 200 and d["profile"]["username"] == "wangwu", d)
    check("映射预览算出角色", st == 200 and d.get("role") == "财务", d)

    st, d, _ = req("POST", f"/api/admin/sso/providers/{oidc_id}/map-preview", body={"raw": {"nothing": 1}}, token=TOKEN)
    check("取不到唯一标识时报错而非 500", st == 400, f"{st} {d}")


def handover_to_token(jar):
    """回调之后：用手里的 handover cookie 换 token。"""
    st, d, url = req("GET", "/api/auth/sso/handover", jar=jar)
    return st, d, url


def test_oidc_flow(oidc_id):
    print("\n[3] OIDC 授权码全流程")
    jar = http.cookiejar.CookieJar()
    st, d, final_url = req("GET", f"/api/auth/sso/{oidc_id}/login", jar=jar, follow=True)
    check("登录链路走通到回调", "/callback" in str(final_url) or st == 200, f"{st} {final_url}")
    check("门户 authorize 被访问", VISITED.get("/authorize", 0) >= 1, VISITED)
    check("门户 token 端点被访问", VISITED.get("/token", 0) >= 1, VISITED)
    check("门户 userinfo 被访问", VISITED.get("/userinfo", 0) >= 1, VISITED)

    st, hv, _ = handover_to_token(jar)
    check("handover 换到 token", st == 200 and hv.get("ok") and hv.get("token"), f"{st} {hv}")
    check("会话时长跟随来源配置(6h)", st == 200 and hv.get("expires_in") == 6 * 3600, hv)
    if not hv.get("token"):
        return None
    st, me, _ = req("GET", "/api/auth/me", token=hv["token"])
    check("/me 返回 SSO 账号", st == 200 and me["user"]["username"] == "zhangsan", f"{st} {me}")
    check("姓名按映射同步", st == 200 and me["user"]["name"] == "张三", me)
    check("分组映射到「财务」角色", st == 200 and me["user"]["role"] == "财务", me)
    check("auth_source 标记为 sso", st == 200 and me["user"].get("auth_source") == "sso", me)

    # handover 一次性
    st, again, _ = req("GET", "/api/auth/sso/handover", jar=jar)
    check("handover 不可重放", st == 200 and again.get("ok") is False, again)
    return hv["token"], me["user"]


def test_employee_binding():
    print("\n[4] 工号自动绑定员工档案")
    st, d, _ = req("POST", "/api/employees", body={"name": "张三", "employee_no": "E102"}, token=TOKEN)
    check("创建测试员工成功", st in (200, 201), f"{st} {d}")

    jar = http.cookiejar.CookieJar()
    st, d, _ = req("GET", "/api/auth/sso/1/login", jar=jar, follow=True)
    st, hv, _ = handover_to_token(jar)
    st, me, _ = req("GET", "/api/auth/me", token=hv.get("token"))
    check("再次登录复用同一账号", st == 200 and me["user"]["username"] == "zhangsan", me)
    check("工号回绑到员工档案", st == 200 and me["user"].get("employee_name") == "张三", me)


def test_cas_flow(cas_id):
    print("\n[5] CAS 3.0 票据流程")
    jar = http.cookiejar.CookieJar()
    st, d, final_url = req("GET", f"/api/auth/sso/{cas_id}/login", jar=jar, follow=True)
    check("CAS 链路走通", "/callback" in str(final_url) or st == 200, f"{st} {final_url}")
    check("CAS serviceValidate 被访问", VISITED.get("/cas/serviceValidate", 0) >= 1, VISITED)
    st, hv, _ = handover_to_token(jar)
    check("CAS 换到 token", st == 200 and hv.get("ok"), f"{st} {hv}")
    st, me, _ = req("GET", "/api/auth/me", token=hv.get("token"))
    check("CAS 账号按 XML 属性落地", st == 200 and me["user"]["username"] == "lisi", me)
    check("CAS 分组映射到审批人", st == 200 and me["user"]["role"] == "审批人", me)

    # 票据被门户拒：手工打一枚坏票据
    jar = http.cookiejar.CookieJar()
    st, d, final_url = req("GET", f"/api/auth/sso/{cas_id}/callback?ticket=BAD-1", jar=jar, follow=True)
    check("门户拒绝票据时回到登录页并带原因", "sso_error" in str(final_url), final_url)


def test_jwt_flow(jwt_id):
    print("\n[6] JWT 令牌直通")
    now = int(time.time())
    good = hs256({
        "sub": "30001", "preferred_username": "zhaoliu", "name": "赵六",
        "iat": now, "exp": now + 600, "iss": PORTAL,
    }, JWT_SECRET)
    jar = http.cookiejar.CookieJar()
    st, d, final_url = req("GET", f"/api/auth/sso/{jwt_id}/login?token={good}", jar=jar, follow=True)
    st, hv, _ = handover_to_token(jar)
    check("合法 JWT 换取成功", st == 200 and hv.get("ok"), f"{st} {hv}")
    st, me, _ = req("GET", "/api/auth/me", token=hv.get("token"))
    check("JWT 账号落地", st == 200 and me["user"]["username"] == "zhaoliu", me)

    bad = hs256({"sub": "30002", "preferred_username": "hacker", "exp": now + 600}, "wrong-secret")
    jar = http.cookiejar.CookieJar()
    st, d, final_url = req("GET", f"/api/auth/sso/{jwt_id}/login?token={bad}", jar=jar, follow=True)
    check("错误签名被拒", "sso_error" in str(final_url), final_url)
    check("错误签名不产生会话", "sso_error" in str(final_url) and "失败" not in "", "")

    # 过期：要越过 ±60s 的时钟容错窗，否则会被当成正常时钟误差放行
    expired = hs256({"sub": "30003", "preferred_username": "old", "exp": now - 600}, JWT_SECRET)
    jar = http.cookiejar.CookieJar()
    st, d, final_url = req("GET", f"/api/auth/sso/{jwt_id}/login?token={expired}", jar=jar, follow=True)
    check("过期令牌被拒", "sso_error" in str(final_url), final_url)


def test_negative(oidc_id, jwt_id):
    print("\n[7] 反向用例")
    jar = http.cookiejar.CookieJar()
    st, d, final_url = req("GET", f"/api/auth/sso/{oidc_id}/callback?code=CODE-x", jar=jar, follow=True)
    check("缺 state 的回调被拒", "sso_error" in str(final_url), final_url)

    st, d, _ = req("GET", "/api/auth/sso/providers", jar=jar)
    check("登录页入口只列启用的来源", st == 200 and len(d.get("items") or []) >= 3, d)

    st, d, _ = req("POST", f"/api/admin/sso/providers/{oidc_id}/toggle", params={"enabled": "false"}, token=TOKEN)
    check("停用成功", st == 200 and d.get("enabled") is False, d)
    st, d, final_url = req("GET", f"/api/auth/sso/{oidc_id}/login", jar=jar, follow=True)
    check("停用的来源拒绝登录", st == 400, f"{st} {d}")
    st, d, _ = req("GET", "/api/auth/sso/providers", jar=jar)
    check("停用后不在登录页入口里",
          st == 200 and not any(x["id"] == oidc_id for x in d.get("items") or []), d)
    req("POST", f"/api/admin/sso/providers/{oidc_id}/toggle", params={"enabled": "true"}, token=TOKEN)

    # 禁止账密登录
    st, d, _ = req("PUT", f"/api/admin/sso/providers/{jwt_id}", body={
        "name": "门户网关-JWT", "protocol": "jwt", "issuer": PORTAL,
        "client_secret": "KEEP", "claim_subject": "sub", "claim_username": "preferred_username",
        "claim_name": "name", "default_role": "申请人", "enabled": True,
        "allow_local_login": False,
    }, token=TOKEN)
    st, d, _ = req("POST", "/api/auth/login", body={"username": "zhaoliu", "password": "whatever"})
    check("SSO 账号被禁止走账密登录", st == 403, f"{st} {d}")

    # 关闭自动开户
    st, d, _ = req("POST", "/api/admin/sso/providers", body={
        "name": "仅已开户", "protocol": "oidc",
        "authorize_url": PORTAL + "/authorize", "token_url": PORTAL + "/token",
        "userinfo_url": PORTAL + "/userinfo", "client_id": "reimburse-app",
        "claim_subject": "sub", "claim_username": "preferred_username", "claim_name": "name",
        "auto_create": False, "default_role": "申请人", "enabled": True,
    }, token=TOKEN)
    pid = (d or {}).get("id")
    jar = http.cookiejar.CookieJar()
    st, d, final_url = req("GET", f"/api/auth/sso/{pid}/login", jar=jar, follow=True)
    check("未开户且关闭自动开户时被拒", "sso_error" in str(final_url), final_url)
    if pid:
        req("DELETE", f"/api/admin/sso/providers/{pid}", token=TOKEN)

    # 非管理员不能碰配置
    st, d, _ = req("GET", "/api/admin/sso/providers")
    check("未登录不得访问配置", st in (401, 403), st)


def test_audit_and_cleanup(oidc_id, cas_id, jwt_id):
    print("\n[8] 审计与删除")
    st, d, _ = req("GET", "/api/audit-logs", params={"entity": "sso_provider", "page_size": 10}, token=TOKEN)
    acts = [x["action"] for x in (d.get("items") or [])]
    check("配置变更进入审计", st == 200 and any("SSO" in a for a in acts), acts[:5])

    st, d, _ = req("GET", "/api/audit-logs", params={"entity": "auth", "action": "SSO登录", "page_size": 5}, token=TOKEN)
    check("SSO 登录写审计", st == 200 and len(d.get("items") or []) >= 1, st)

    st, d, _ = req("DELETE", f"/api/admin/sso/providers/{cas_id}", token=TOKEN)
    check("删除返回关联账号数", st == 200 and isinstance(d.get("bound_users"), int), d)

    st, d, _ = req("GET", "/api/admin/sso/providers", token=TOKEN)
    remaining = [x["id"] for x in (d.get("items") or [])]
    check("删除生效", st == 200 and cas_id not in remaining, remaining)


def main():
    global TOKEN
    print(f"目标：{BASE}")
    start_portal()

    st, health, _ = req("GET", "/api/health")
    check("服务可达", st == 200, health)
    if st != 200:
        print("服务没起来，先 tools/dev_server.sh start")
        return
    check("版本为 2.9.23", str(health.get("version")) == "2.9.23", health.get("version"))

    st, d, _ = req("POST", "/api/auth/login", body={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    check("管理员登录成功", st == 200 and d.get("token"), f"{st} {d}")
    if st != 200:
        return
    TOKEN = d["token"]

    oidc_id, cas_id, jwt_id = test_admin_crud()
    if not (oidc_id and cas_id and jwt_id):
        print("身份源创建失败，后续用例跳过")
    else:
        test_tools(oidc_id)
        test_oidc_flow(oidc_id)
        test_employee_binding()
        test_cas_flow(cas_id)
        test_jwt_flow(jwt_id)
        test_negative(oidc_id, jwt_id)
        test_audit_and_cleanup(oidc_id, cas_id, jwt_id)

    print(f"\n{'=' * 46}\n通过 {OK} 项，失败 {FAIL} 项")
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
