"""单点登录协议引擎（v2.9.23）。

支持的协议（都由管理员在「单点登录」页配置，本模块只负责跑流程）：

    oidc    OpenID Connect：授权码 + id_token 校验 + userinfo
    oauth2  OAuth2 授权码：token + userinfo（没有 id_token 也能跑）
    cas3    CAS 3.0：serviceValidate 回调 XML
    jwt     令牌直通：门户签发 JWT，通过 URL 参数或 Header 直接传进来

三条硬约束（与项目一贯的技术取舍一致）：
1. **零第三方依赖**：HTTP 走 stdlib urllib，JWT 验签走 hmac/hashlib，
   RS256 的公钥一律用 int.from_bytes + pow 自己做 PKCS#1 v1.5 校验。
   不引 PyJWT / cryptography —— 生产镜像要多一层编译依赖，ARM 上翻过车。
2. **错误必须留痕**：任何一步失败都写进 last_error()（HTTP 状态 + 服务端 message +
   可读提示），管理员点「连通性测试」要能看到根因，而不是一句「调用失败」。
3. **资料处理与协议解耦**：所有协议最后都归一成同一个 Profile dict，
   字段从哪来（userinfo / id_token / CAS 属性）由 claims 配置决定，上层不关心。

Profile 结构：
    {
      "subject":    门户侧唯一 id（必填，用来绑定本地账号）
      "username":   登录名
      "name":       姓名
      "employee_no": 工号（用于自动绑定 Employee）
      "department": 部门名（目前只做存档与排障展示）
      "email" / "phone" / "groups":[...]
      "raw":        原始响应（脱敏后，给管理员做字段映射预览）
    }
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from . import crypt
from . import models as m

DEFAULT_TIMEOUT = 8.0

# JWKS 缓存：门户公钥不会频繁轮转，缓存 5 分钟，避免每次登录都多一次网络往返
_JWKS_CACHE: dict[str, tuple[float, dict]] = {}
_JWKS_TTL = 300.0

# 最近一次失败的详细情况（连通性测试 / 排障用）
_last_error: dict[str, Any] = {}
_last_meta: dict[str, Any] = {}


class SsoError(RuntimeError):
    """SSO 流程里的可预期失败：消息直接给用户看。"""


# ------------------------------------------------------------------ 错误留痕
def _fail(code: str, message: str, **extra: Any) -> SsoError:
    """记录失败上下文再抛异常。

    早期版本把异常全吞掉只留一句话，管理员点测试只看到「调用失败」，
    排查成本全转嫁给用户 —— 所以这里强制带上 code / http_status / 服务端返回片段。
    """
    _last_error.clear()
    _last_error.update({"code": code, "message": message, "at": time.time()})
    _last_error.update(extra)
    return SsoError(message)


def last_error(clear: bool = False) -> dict[str, Any]:
    """给连通性测试用的最后一次错误详情。"""
    out = dict(_last_error)
    if clear:
        _last_error.clear()
    return out


def last_meta(clear: bool = False) -> dict[str, Any]:
    out = dict(_last_meta)
    if clear:
        _last_meta.clear()
    return out


def _note_meta(**kw: Any) -> None:
    _last_meta.clear()
    _last_meta.update(kw)


# ------------------------------------------------------------------ HTTP
def _ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    basic_auth: tuple[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    verify_ssl: bool = True,
) -> tuple[int, dict[str, str], str]:
    """返回 (status, headers, text)。不抛 HTTPError，让调用方自己判状态码。

    为什么自己判：400/401 的响应体里往往有门户给的具体原因
    （invalid_client / unauthorized_client），吞掉就只剩一句「登录失败」。
    """
    body = None
    hdrs = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if basic_auth:
        pair = f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")
        hdrs["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
    hdrs.setdefault("Accept", "application/json, text/xml, */*")
    hdrs.setdefault("User-Agent", "reimburse-workbench/sso")

    req = urllib.request.Request(url, data=body, method=method)
    for k, v in hdrs.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(verify_ssl)) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, dict(resp.headers.items()), raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            pass
        return e.code, dict(e.headers.items() or {}), raw.decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001  超时 / DNS / TLS
        raise _fail(
            "http_error",
            f"无法连接统一门户：{type(e).__name__} {e}",
            url=url,
        ) from e


def _json_of(text: str) -> dict:
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"data": obj}
    except (json.JSONDecodeError, TypeError):
        return {}


# ------------------------------------------------------------------ JWT
def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _jwt_parts(token: str) -> tuple[dict, dict, bytes, str]:
    """拆 JWT，返回 (header, payload, 签名原始字节, 签名输入串)。不验签。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise _fail("jwt_format", "令牌格式不正确：不是 JWT（缺少分段）")
    head_raw, payload_raw, sig = parts
    try:
        header = json.loads(_b64url_decode(head_raw) or b"{}")
        payload = json.loads(_b64url_decode(payload_raw) or b"{}")
    except Exception as e:  # noqa: BLE001
        raise _fail("jwt_format", f"令牌解析失败：{e}") from e
    return header, payload, _b64url_decode(sig), f"{head_raw}.{payload_raw}"


_SHA256_DER_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _der_parse(raw: bytes, pos: int = 0) -> tuple[int, Any, int]:
    """极简 DER 解析器，返回 (tag, 值或子节点列表, 下一个字节的位置)。

    只需要够用：从 SubjectPublicKeyInfo 里抠 RSA 的 n / e。
    构造类型（0x20）会递归解析子节点，其余按原始字节返回。
    """
    tag = raw[pos]
    pos += 1
    length = raw[pos]
    pos += 1
    if length & 0x80:
        nbytes = length & 0x7F
        length = int.from_bytes(raw[pos:pos + nbytes], "big")
        pos += nbytes
    end = pos + length
    value: Any = raw[pos:end]
    if tag & 0x20:  # constructed
        children: list[tuple[int, Any, int]] = []
        cp = 0
        while cp < len(value):
            node = _der_parse(value, cp)
            children.append(node)
            cp = node[2]
        return tag, children, end
    return tag, value, end


def _rsa_public_from_spki(pem: str) -> tuple[int, int] | None:
    """从 PEM 公钥里取出 (n, e)。支持 SPKI（-----BEGIN PUBLIC KEY-----）。"""
    body = "".join(
        ln for ln in pem.strip().splitlines() if ln and not ln.startswith("-----")
    )
    try:
        raw = base64.b64decode(body)
        _tag, children, _ = _der_parse(raw, 0)          # SubjectPublicKeyInfo
        if not isinstance(children, list) or len(children) < 2:
            return None
        bitstring = children[1][1]
        key_bytes = bitstring[1:] if isinstance(bitstring, bytes) else b""   # 去掉「未使用位数」
        _tag2, key_children, _ = _der_parse(key_bytes, 0)                    # RSAPublicKey
        if not isinstance(key_children, list) or len(key_children) < 2:
            return None
        n_bytes = key_children[0][1]
        e_bytes = key_children[1][1]
        if not isinstance(n_bytes, bytes) or not isinstance(e_bytes, bytes):
            return None
        return (
            int.from_bytes(n_bytes, "big"),
            int.from_bytes(e_bytes, "big"),
        )
    except Exception:  # noqa: BLE001
        return None


def _jwk_to_n_e(jwk: dict) -> tuple[int, int] | None:
    if jwk.get("kty") != "RSA":
        return None
    n_b64, e_b64 = jwk.get("n"), jwk.get("e")
    if not n_b64 or not e_b64:
        return None
    try:
        return (
            int.from_bytes(_b64url_decode(n_b64), "big"),
            int.from_bytes(_b64url_decode(e_b64), "big"),
        )
    except Exception:  # noqa: BLE001
        return None


def fetch_jwks(url: str, *, timeout: float = DEFAULT_TIMEOUT, verify_ssl: bool = True) -> dict:
    cached = _JWKS_CACHE.get(url)
    now = time.time()
    if cached and now - cached[0] < _JWKS_TTL:
        return cached[1]
    status, _h, text = _http(url, timeout=timeout, verify_ssl=verify_ssl)
    if status != 200:
        raise _fail("jwks_failed", f"拉取公钥失败（HTTP {status}）", http_status=status, response=text[:300])
    doc = _json_of(text)
    if not doc.get("keys"):
        raise _fail("jwks_empty", "公钥接口没有返回 keys", response=text[:300])
    _JWKS_CACHE[url] = (now, doc)
    return doc


def _verify_rs256(signing_input: str, signature: bytes, n: int, e: int) -> bool:
    """纯 Python 的 RSASSA-PKCS1-v1_5 / SHA-256 校验。

    原理：s^e mod n 还原出 padded EMSA 签名块，和「0x00 01 FF.. 00 DER‖SHA256」
    逐字节比对。公钥运算只有一次 pow，成本可以忽略。
    """
    digest_info = _SHA256_DER_PREFIX + hashlib.sha256(signing_input.encode("ascii")).digest()
    k = (n.bit_length() + 7) // 8
    if k < len(digest_info) + 11 or not n:
        return False
    try:
        em = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    except Exception:  # noqa: BLE001
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    return hmac.compare_digest(em, expected)


def _verify_hs256(signing_input: str, signature: bytes, secret: str) -> bool:
    expect = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return hmac.compare_digest(expect, signature)


def verify_jwt(
    token: str,
    *,
    secret: str = "",
    jwks_url: str = "",
    issuer: str = "",
    audience: str = "",
    leeway: int = 60,
    timeout: float = DEFAULT_TIMEOUT,
    verify_ssl: bool = True,
) -> dict:
    """校验并返回 claims。HS256 用 secret，RS256 走 JWKS（或 PEM 公钥）。

    默认校验 iss / exp / nbf；aud 只在显式给了 audience 时校验
    —— 门户给的 aud 常常带多个取值或干脆不发，一律校验会在那边拦死。
    """
    header, payload, signature, signing_input = _jwt_parts(token)
    alg = (header.get("alg") or "").upper()
    now = time.time()

    if alg == "HS256":
        if not secret:
            raise _fail("missing_secret", "令牌是 HS256 但没有配置验签密钥")
        if not _verify_hs256(signing_input, signature, secret):
            raise _fail("bad_signature", "令牌签名校验失败（HS256 密钥不匹配）")
    elif alg in ("RS256", "RSA"):
        key = None
        kid = header.get("kid")
        source = ""
        if jwks_url:
            # jwks_url 里允许直接贴 PEM 公钥（自研门户常见）：省得再起一个公钥服务
            if "BEGIN PUBLIC KEY" in jwks_url or "BEGIN RSA PUBLIC KEY" in jwks_url:
                key = _rsa_public_from_spki(jwks_url)
                source = "pem"
            else:
                doc = fetch_jwks(jwks_url, timeout=timeout, verify_ssl=verify_ssl)
                keys = doc.get("keys") or []
                picked = None
                for k in keys:
                    if kid and k.get("kid") == kid:
                        picked = k
                        break
                    if not kid and k.get("kty") == "RSA":
                        picked = k
                        break
                if picked is None:
                    raise _fail(
                        "no_key",
                        f"公钥集中找不到 kid={kid or '-'} 对应的 RSA 公钥",
                        kid=kid, available=[k.get("kid") for k in keys][:10],
                    )
                key = _jwk_to_n_e(picked)
                source = "jwks"
        if key is None:
            raise _fail("missing_key", "令牌是 RS256 但没有配置公钥（JWKS 地址或 PEM 公钥）")
        if not _verify_rs256(signing_input, signature, key[0], key[1]):
            raise _fail("bad_signature", f"令牌签名校验失败（RS256，公钥来源 {source}）")
    else:
        raise _fail("unsupported_alg", f"不支持的签名算法 {alg or '未知'}（仅支持 HS256 / RS256）")

    if issuer and payload.get("iss") and payload["iss"] != issuer:
        raise _fail("bad_issuer", f"令牌签发方不匹配：期望 {issuer}，实际 {payload.get('iss')}")
    exp = payload.get("exp")
    if exp is not None and now > float(exp) + leeway:
        raise _fail("token_expired", "令牌已过期", exp=exp)
    nbf = payload.get("nbf")
    if nbf is not None and now + leeway < float(nbf):
        raise _fail("token_not_yet", "令牌尚未生效", nbf=nbf)
    if audience:
        aud = payload.get("aud")
        aud_list = aud if isinstance(aud, list) else ([aud] if aud else [])
        if audience not in aud_list:
            raise _fail("bad_audience", f"令牌受众不匹配：期望 {audience}，实际 {aud}")

    _note_meta(alg=alg, kid=header.get("kid"), claims=list(payload.keys()))
    return payload


# ------------------------------------------------------------------ 字段映射
def _dig(raw: dict, path: str) -> Any:
    """支持 `a.b.c` 的路径取值；末端找不到时做一次不区分大小写的兜底。

    为什么兜底：门户返回的键大小写非常随意（UserName / username / USERNAME），
    管理员照着文档填了一个，换环境就取不到；这里退一步能救回大部分配置错误。
    """
    if not path:
        return None
    cur: Any = raw
    for part in path.split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            hit = next((v for k, v in cur.items() if k.lower() == part.lower()), None)
            if hit is None:
                return None
            cur = hit
        else:
            return None
    if isinstance(cur, (list, tuple, dict)):
        return cur
    return cur


def _first_str(raw: dict, *paths: str) -> str | None:
    for p in paths:
        val = _dig(raw, p)
        if isinstance(val, (list, tuple)):
            val = val[0] if val else None
        if val is None or val == "":
            continue
        return str(val).strip()
    return None


def _as_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        items = list(val)
    elif isinstance(val, str):
        # CAS / 门户常见把多值塞成逗号分隔字符串
        items = [p for p in val.replace(";", ",").split(",")]
    else:
        items = [val]
    return [str(i).strip() for i in items if str(i).strip()]


def normalize_profile(p: m.SsoProvider, raw: dict) -> dict:
    """把门户返回的原始字段映射成本系统 Profile。"""
    subject = _first_str(raw, p.claim_subject or "sub")
    if not subject:
        raise _fail(
            "no_subject",
            f"响应里取不到唯一标识字段 {p.claim_subject or 'sub'}，"
            f"可用字段：{', '.join(list(raw.keys())[:12]) or '（空）'}",
            fields=list(raw.keys()),
        )
    username = _first_str(
        raw, p.claim_username or "preferred_username", "username", "user_name", "account", p.claim_subject
    ) or subject
    name = _first_str(raw, p.claim_name or "name", "display_name", "displayName", "nickname") or username
    return {
        "subject": str(subject),
        "username": username,
        "name": name[:32],
        "employee_no": _first_str(raw, p.claim_employee_no or "", "employee_no", "employeeNo", "work_no"),
        "department": _first_str(raw, p.claim_department or "", "department", "dept_name", "org_name"),
        "email": _first_str(raw, p.claim_email or "", "email", "mail"),
        "phone": _first_str(raw, p.claim_phone or "", "phone", "phone_number", "mobile"),
        "groups": _as_list(_dig(raw, p.claim_groups or "")),
        "raw": _sanitize_raw(raw),
    }


_SENSITIVE_HINTS = ("token", "secret", "password", "passwd", "assertion", "ticket")


def _sanitize_raw(raw: dict) -> dict:
    """给管理员做「字段映射预览」用的原始结构：只留标量/浅列表，凭据类字段一律打码。"""
    out: dict[str, Any] = {}
    for k, v in list(raw.items())[:40]:
        low = k.lower()
        if isinstance(v, (str, int, float, bool)) or v is None:
            if any(h in low for h in _SENSITIVE_HINTS) and isinstance(v, str) and v:
                out[k] = crypt.mask(v)
            else:
                out[k] = v if not isinstance(v, str) else v[:120]
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x)[:60] for x in list(v)[:10]]
        elif isinstance(v, dict):
            out[k] = "{...}"
    return out


# ------------------------------------------------------------------ 发起登录
def build_authorize_url(p: m.SsoProvider, redirect_uri: str, state: str, pkce_challenge: str = "") -> str:
    """拼门户登录地址。返回浏览器要跳转的完整 URL。"""
    if p.protocol in (m.PROTO_OAUTH2, m.PROTO_OIDC):
        base = (p.authorize_url or "").strip()
        if not base:
            raise _fail("no_authorize_url", "没有配置授权地址（authorize_url）")
        params = {
            "response_type": "code",
            "client_id": (p.client_id or "").strip(),
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": (p.scope or "openid profile email").strip(),
        }
        if p.protocol == m.PROTO_OIDC:
            params.setdefault("nonce", secrets.token_urlsafe(12))
        if pkce_challenge:
            params["code_challenge"] = pkce_challenge
            params["code_challenge_method"] = "S256"
        return f"{base}{'&' if '?' in base else '?'}{urllib.parse.urlencode(params)}"

    if p.protocol == m.PROTO_CAS:
        base = (p.authorize_url or "").strip()
        if not base:
            raise _fail("no_cas_login_url", "没有配置 CAS 登录地址（authorize_url）")
        return f"{base}{'&' if '?' in base else '?'}{urllib.parse.urlencode({'service': redirect_uri})}"

    if p.protocol == m.PROTO_JWT:
        raise _fail("jwt_no_redirect", "JWT 直通模式不需要跳转，由门户侧直接携带令牌访问本系统")

    raise _fail("bad_protocol", f"不支持的协议 {p.protocol}")


def new_pkce() -> tuple[str, str]:
    """生成 PKCE verifier / challenge（S256）。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode().rstrip("=")
    return verifier, challenge


# ------------------------------------------------------------------ 回调解析
def exchange_code(p: m.SsoProvider, code: str, redirect_uri: str, pkce_verifier: str = "") -> dict:
    """授权码换 access_token（OAuth2 / OIDC 共用）。"""
    token_url = (p.token_url or "").strip()
    if not token_url:
        raise _fail("no_token_url", "没有配置令牌地址（token_url）")
    secret = crypt.decrypt(p.client_secret_enc)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": (p.client_id or "").strip(),
    }
    if pkce_verifier:
        data["code_verifier"] = pkce_verifier
    # 有密钥就放 Basic（RFC 6749 推荐），门户不支持时下面会退到 post body 重发一次
    status, _h, text = _http(
        token_url, method="POST", data=data,
        basic_auth=((p.client_id or ""), secret) if secret else None,
        timeout=p.timeout_sec or DEFAULT_TIMEOUT, verify_ssl=p.verify_ssl,
    )
    if status != 200 and secret and "client_secret" not in " ".join(data.keys()):
        data["client_secret"] = secret
        status, _h, text = _http(
            token_url, method="POST", data=data,
            timeout=p.timeout_sec or DEFAULT_TIMEOUT, verify_ssl=p.verify_ssl,
        )
    doc = _json_of(text)
    if status != 200:
        raise _fail(
            "token_failed",
            f"换取令牌失败（HTTP {status}）：{doc.get('error_description') or doc.get('error') or text[:200]}",
            http_status=status, response=text[:400],
        )
    if not doc.get("access_token"):
        raise _fail("no_access_token", "令牌响应里没有 access_token", response=text[:300])
    return doc


def _id_token_claims(p: m.SsoProvider, token_resp: dict) -> dict:
    """OIDC 的 id_token：验签后当成 userinfo 的补充来源（claims 优先级更高）。"""
    id_token = token_resp.get("id_token")
    if not id_token or not isinstance(id_token, str):
        return {}
    return verify_jwt(
        id_token,
        secret=crypt.decrypt(p.jwt_secret_enc) or crypt.decrypt(p.client_secret_enc),
        jwks_url=(p.jwks_url or "").strip(),
        issuer=(p.issuer or "").strip(),
        audience=(p.client_id or "").strip() if p.protocol == m.PROTO_OIDC else "",
        timeout=p.timeout_sec or DEFAULT_TIMEOUT,
        verify_ssl=p.verify_ssl,
    )


def fetch_userinfo(p: m.SsoProvider, access_token: str) -> dict:
    url = (p.userinfo_url or "").strip()
    if not url:
        return {}
    status, _h, text = _http(
        url, headers={"Authorization": "Bearer " + access_token},
        timeout=p.timeout_sec or DEFAULT_TIMEOUT, verify_ssl=p.verify_ssl,
    )
    doc = _json_of(text)
    if status != 200:
        raise _fail(
            "userinfo_failed",
            f"拉取用户信息失败（HTTP {status}）：{doc.get('error_description') or doc.get('message') or text[:200]}",
            http_status=status, response=text[:300],
        )
    return doc


def _cas_attributes(p: m.SsoProvider, ticket: str, redirect_uri: str) -> dict:
    """CAS serviceValidate：把 XML 解析成扁平 dict。"""
    base = (p.validate_url or "").strip()
    if not base:
        raise _fail("no_validate_url", "没有配置票据校验地址（validate_url）")
    url = f"{base}{'&' if '?' in base else '?'}{urllib.parse.urlencode({'service': redirect_uri, 'ticket': ticket, 'format': 'XML'})}"
    status, _h, text = _http(url, timeout=p.timeout_sec or DEFAULT_TIMEOUT, verify_ssl=p.verify_ssl)
    if status != 200:
        raise _fail("cas_failed", f"票据校验失败（HTTP {status}）", http_status=status, response=text[:300])
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise _fail("cas_bad_xml", f"票据校验返回的不是合法 XML：{e}", response=text[:300]) from e

    def local(tag: str) -> str:
        return tag.split("}")[-1]

    success = None
    failure = None
    for node in root.iter():
        if local(node.tag) == "authenticationSuccess":
            success = node
        elif local(node.tag) == "authenticationFailure":
            failure = node
    if failure is not None:
        raise _fail(
            "cas_rejected",
            f"门户拒绝了票据：{failure.get('code') or ''} {text[:200]}".strip(),
            response=text[:300],
        )
    if success is None:
        raise _fail("cas_no_success", "票据校验响应里没有 authenticationSuccess", response=text[:300])

    out: dict[str, Any] = {}
    for child in success:
        tag = local(child.tag)
        if tag == "attributes":
            for attr in child:
                key = local(attr.tag)
                # CAS 多值属性会给多个同名节点
                if key in out:
                    prev = out[key]
                    out[key] = (list(prev) if isinstance(prev, list) else [str(prev)])
                    out[key].append(attr.text or "")
                else:
                    out[key] = attr.text or ""
        else:
            out[tag] = child.text or ""
    # 兜底：没人配 subject claim 时用 CAS 的 user 字段
    out.setdefault("cas_user", out.get("user") or "")
    return out


def resolve_profile(
    p: m.SsoProvider,
    query: dict[str, Any],
    *,
    redirect_uri: str = "",
    headers: Any = None,
    pkce_verifier: str = "",
) -> dict:
    """把门户回调（或带令牌的请求）解析成 Profile。唯一入口。"""
    if p.protocol in (m.PROTO_OAUTH2, m.PROTO_OIDC):
        code = query.get("code")
        if not code:
            raise _fail("no_code", "门户回调里没有授权码 code")
        token_resp = exchange_code(p, str(code), redirect_uri, pkce_verifier)
        raw: dict = {}
        try:
            claims = _id_token_claims(p, token_resp)
            raw.update(claims)
        except SsoError:
            # OIDC 才有 id_token；oauth2 到这里没有是常态，用 userinfo 兜
            if p.protocol == m.PROTO_OIDC:
                raise
        try:
            info = fetch_userinfo(p, token_resp["access_token"])
            raw.update(info or {})
        except SsoError:
            if not raw:
                raise
        return normalize_profile(p, raw)

    if p.protocol == m.PROTO_CAS:
        ticket = query.get("ticket")
        if not ticket:
            raise _fail("no_ticket", "CAS 回调里没有票据 ticket")
        return normalize_profile(p, _cas_attributes(p, str(ticket), redirect_uri))

    if p.protocol == m.PROTO_JWT:
        token = ""
        if (p.jwt_source or "query") == "header":
            name = (p.jwt_param or "token").strip()
            token = (headers.get(name) if headers else None) or ""
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
        else:
            token = str(query.get((p.jwt_param or "token").strip()) or "")
        if not token:
            raise _fail(
                "no_token",
                f"请求里没有令牌参数 {p.jwt_param or 'token'}"
                + ("（Header）" if (p.jwt_source or "query") == "header" else "（URL 参数）"),
            )
        claims = verify_jwt(
            token,
            secret=crypt.decrypt(p.jwt_secret_enc) or crypt.decrypt(p.client_secret_enc),
            jwks_url=(p.jwks_url or "").strip(),
            issuer=(p.issuer or "").strip(),
            audience=(p.jwt_audience or "").strip(),
            timeout=p.timeout_sec or DEFAULT_TIMEOUT,
            verify_ssl=p.verify_ssl,
        )
        return normalize_profile(p, claims)

    raise _fail("bad_protocol", f"不支持的协议 {p.protocol}")


# ------------------------------------------------------------------ 连通性测试
def discover(p: m.SsoProvider | None = None, issuer: str = "", timeout: float = DEFAULT_TIMEOUT,
             verify_ssl: bool = True) -> dict:
    """OIDC 自动发现：拉 /.well-known/openid-configuration 回填端点。"""
    base = (issuer or (p.issuer if p else "") or "").strip().rstrip("/")
    if not base:
        raise _fail("no_issuer", "请先填写 issuer（签发方地址）")
    url = base + "/.well-known/openid-configuration"
    status, _h, text = _http(url, timeout=timeout, verify_ssl=verify_ssl)
    doc = _json_of(text)
    if status != 200 or not doc:
        raise _fail(
            "discover_failed",
            f"自动发现失败（HTTP {status}）：门户没有提供 OIDC 配置文档",
            http_status=status, response=text[:300],
        )
    out = {
        "issuer": doc.get("issuer") or base,
        "authorize_url": doc.get("authorization_endpoint") or "",
        "token_url": doc.get("token_endpoint") or "",
        "userinfo_url": doc.get("userinfo_endpoint") or "",
        "jwks_url": doc.get("jwks_uri") or "",
        "scopes_supported": doc.get("scopes_supported") or [],
        "claims_supported": doc.get("claims_supported") or [],
        "end_session_endpoint": doc.get("end_session_endpoint") or "",
    }
    _note_meta(discovered=out)
    return out


def test_connection(p: m.SsoProvider) -> dict:
    """连通性自检：只看「配得够不够、门户通不通」，不要求真的完成一次登录。

    每一步都把门户的返回首帧放进 errors，管理员不用翻容器日志。
    """
    checks: list[dict] = []
    notes: list[str] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            notes.append(detail)

    if p.protocol in (m.PROTO_OAUTH2, m.PROTO_OIDC):
        for label, url in (("授权地址", p.authorize_url), ("令牌地址", p.token_url), ("用户信息地址", p.userinfo_url)):
            if not (url or "").strip():
                add(label, False, f"{label}未配置")
                continue
            try:
                status, _h, text = _http(
                    url, method="GET", timeout=p.timeout_sec or DEFAULT_TIMEOUT, verify_ssl=p.verify_ssl
                )
            except SsoError as e:
                add(label, False, f"{label}不可达：{e}")
                continue
            # 401/405 也算通：端点在，只是不允许 GET
            add(label, status < 500, f"{label} HTTP {status}（端点可达）")
            if status >= 500 and text:
                notes.append(f"{label}返回：{text[:160]}")
        if p.jwks_url:
            try:
                doc = fetch_jwks(p.jwks_url, timeout=p.timeout_sec or DEFAULT_TIMEOUT, verify_ssl=p.verify_ssl)
                add("公钥 JWKS", True, f"取到 {len(doc.get('keys') or [])} 个公钥")
            except SsoError as e:
                add("公钥 JWKS", False, str(e))
        if not (p.client_id or "").strip():
            add("client_id", False, "未配置 client_id")
        if not crypt.decrypt(p.client_secret_enc):
            # 公开的 native client 可以没有密钥，只提示不判失败
            notes.append("未配置 client_secret：若门户要求机密客户端，这里会换取令牌失败")
    elif p.protocol == m.PROTO_CAS:
        for label, url in (("CAS 登录地址", p.authorize_url), ("票据校验地址", p.validate_url)):
            if not (url or "").strip():
                add(label, False, f"{label}未配置")
                continue
            try:
                status, _h, text = _http(url, timeout=p.timeout_sec or DEFAULT_TIMEOUT, verify_ssl=p.verify_ssl)
            except SsoError as e:
                add(label, False, f"{label}不可达：{e}")
                continue
            add(label, status < 500, f"{label} HTTP {status}")
            if status >= 500 and text:
                notes.append(f"{label}返回：{text[:160]}")
    elif p.protocol == m.PROTO_JWT:
        has_secret = bool(crypt.decrypt(p.jwt_secret_enc) or crypt.decrypt(p.client_secret_enc))
        has_key = bool((p.jwks_url or "").strip())
        add("验签凭据", has_secret or has_key,
            "已配置 HS256 密钥" if has_secret else ("已配置公钥" if has_key else "既没配密钥也没配公钥"))
        if has_key and not has_secret:
            try:
                if "BEGIN" in (p.jwks_url or ""):
                    ok = _rsa_public_from_spki(p.jwks_url) is not None
                    add("公钥解析", ok, "PEM 公钥解析成功" if ok else "PEM 公钥解析失败，请检查是否复制完整")
                else:
                    keys = (fetch_jwks(p.jwks_url, timeout=p.timeout_sec or DEFAULT_TIMEOUT,
                                       verify_ssl=p.verify_ssl).get("keys") or [])
                    add("公钥 JWKS", bool(keys), f"取到 {len(keys)} 个公钥")
            except SsoError as e:
                add("公钥 JWKS", False, str(e))
    else:
        add("协议", False, f"不支持的协议 {p.protocol}")

    ok = all(c["ok"] for c in checks)
    _note_meta(checks=checks, notes=notes, protocol=p.protocol)
    return {"ok": ok, "checks": checks, "notes": notes, "error": last_error()}


# ------------------------------------------------------------------ 账号落地
def role_map(p: m.SsoProvider) -> dict[str, str]:
    try:
        obj = json.loads(p.role_map or "{}")
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def mapped_role(p: m.SsoProvider, profile: dict) -> str:
    """按门户分组落到本系统角色。没匹配上就用 default_role。"""
    mapping = role_map(p)
    for group in profile.get("groups") or []:
        role = mapping.get(group)
        if role in m.ROLES:
            return role
        # 大小写 / 空格差异也认，门户那边的组名很难保证和规范完全一致
        role = next((v for k, v in mapping.items() if k.strip().lower() == str(group).strip().lower()), None)
        if role in m.ROLES:
            return role
    return p.default_role or m.ROLE_APPLICANT


def _unique_username(db, base: str) -> str:
    base = "".join(ch for ch in (base or "").strip() if ch not in "/\\:@\"' ,;")[:48] or "sso"
    if not db.query(m.AppUser).filter(m.AppUser.username == base).first():
        return base
    for i in range(2, 50):
        cand = f"{base}{i}"
        if not db.query(m.AppUser).filter(m.AppUser.username == cand).first():
            return cand
    return f"{base}{secrets.token_hex(3)}"


def _sync_profile(db, user: m.AppUser, p: m.SsoProvider, profile: dict) -> None:
    """登录后同步资料。

    只做「补齐」不做「覆盖」：员工表里的人事字段归 hr 管，
    SSO 只负责把门户那边有的（姓名、工号绑定、缺失的邮箱/手机）补进来，
    已经填过的一律不动。
    """
    if profile.get("name") and profile["name"] != user.name:
        user.name = profile["name"][:32]

    emp = None
    if profile.get("employee_no"):
        emp = (
            db.query(m.Employee)
            .filter(m.Employee.employee_no == profile["employee_no"])
            .first()
        )
        if emp and user.employee_id != emp.id:
            user.employee_id = emp.id
    if user.employee_id:
        emp = emp or db.get(m.Employee, user.employee_id)

    if emp and p.sync_profile:
        if not emp.email and profile.get("email"):
            emp.email = profile["email"][:128]
        if not emp.phone and profile.get("phone"):
            emp.phone = profile["phone"][:32]


def resolve_or_create_user(db, p: m.SsoProvider, profile: dict) -> m.AppUser:
    """把门户身份解析成本地账号。三种落地路径：

    1. (provider, subject) 已绑定 —— 每次都走这条，稳。
    2. 开关允许时按登录名归到本地同名账号 —— **管理员账号永不在此列**（防止门户侧的普通用户
       借同名蹭到管理员权限）。首次归户后走的就是绑定关系，后续不再比对用户名。
    3. 都没命中且开了自动开户 —— 新建本地账号（无本地密码）。
    """
    user = (
        db.query(m.AppUser)
        .filter(
            m.AppUser.sso_provider_id == p.id,
            m.AppUser.sso_subject == profile["subject"],
        )
        .first()
    )
    if user:
        if not user.active:
            raise _fail("user_inactive", "该账号已被停用，请联系管理员")
        if p.sync_profile:
            _sync_profile(db, user, p, profile)
        return user

    username = (profile.get("username") or profile["subject"]).strip()
    local = db.query(m.AppUser).filter(m.AppUser.username == username).first()
    if local and p.bind_local_by_username and local.role != m.ROLE_ADMIN:
        local.auth_source = m.AUTH_SSO
        local.sso_provider_id = p.id
        local.sso_subject = profile["subject"]
        _sync_profile(db, local, p, profile)
        db.flush()
        return local

    if not p.auto_create:
        raise _fail(
            "no_account",
            f"门户账号 {username} 在本系统还没有对应账号，且该来源未开启自动开户，请联系管理员",
        )

    user = m.AppUser(
        username=_unique_username(db, username),
        password_hash=m.SSO_NO_PASSWORD,   # 没有本地密码，账密登录必然失败
        name=(profile.get("name") or username)[:32],
        role=mapped_role(p, profile),
        active=True,
        must_change_password=False,
        auth_source=m.AUTH_SSO,
        sso_provider_id=p.id,
        sso_subject=profile["subject"],
    )
    db.add(user)
    db.flush()
    _sync_profile(db, user, p, profile)
    return user
