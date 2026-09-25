"""对称加解密：只用标准库，给「需要回读」的凭据用（邮箱登录密码）。

为什么不用哈希：IMAP 登录必须拿到明文密码，所以只能可逆加密。
为什么不用 cryptography/passlib：本项目坚持零编译依赖，老 Docker 引擎上装包会翻车。

算法：HMAC-SHA256 派生密钥流做 XOR（CTR 式），再附一段 HMAC 做完整性校验。
对「数据库被拖库」的场景足够——攻击者拿不到 SECRET_KEY 就解不开；
拿得到 SECRET_KEY 也就能拿到同一台机器上的 env，属于同一信任边界。

密文格式：`v1$<b64 nonce>$<b64 ciphertext>$<b64 mac>`
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

_PREFIX = "v1"
_INFO = b"wb-reimburse/mail-credential/v1"
_ROUNDS = 120_000
_DEFAULT_SECRET = "wb-dev-insecure-mail-secret"


class SecretMissing(RuntimeError):
    """生产环境没有配置 MAIL_SECRET 时抛出。"""


def get_secret() -> str:
    """取加密主密钥。

    生产请在 .env 里显式配置 MAIL_SECRET；开发环境回落到固定值并打印一次告警，
    免得本机调试时因为忘记配环境变量而无法保存邮箱账号。
    """
    secret = (os.getenv("MAIL_SECRET") or "").strip()
    if secret:
        return secret
    if (os.getenv("ENV") or os.getenv("APP_ENV") or "").lower() in ("prod", "production"):
        raise SecretMissing("生产环境必须配置 MAIL_SECRET，否则邮箱密码无法安全保存")
    if not getattr(get_secret, "_warned", False):
        print("[warn] 未配置 MAIL_SECRET，正在使用开发默认密钥；生产部署务必设置")
        get_secret._warned = True  # type: ignore[attr-defined]
    return _DEFAULT_SECRET


def _key(secret: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), _INFO, _ROUNDS, dklen=32)


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def encrypt(plain: str, secret: str | None = None) -> str:
    if plain is None:
        plain = ""
    key = _key(secret or get_secret())
    nonce = secrets.token_bytes(16)
    data = plain.encode("utf-8")
    stream = _keystream(key, nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    return f"{_PREFIX}${_b64e(nonce)}${_b64e(cipher)}${_b64e(mac)}"


def decrypt(token: str | None, secret: str | None = None) -> str:
    """解不开时返回空串，让调用方给出「密码需重填」而不是 500。"""
    if not token:
        return ""
    try:
        prefix, nonce_b64, data_b64, mac_b64 = token.split("$")
        if prefix != _PREFIX:
            return ""
        key = _key(secret or get_secret())
        nonce = _b64d(nonce_b64)
        cipher = _b64d(data_b64)
        mac = _b64d(mac_b64)
        expect = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(mac, expect):
            # 换过 MAIL_SECRET 或库被改过：只能重填密码
            return ""
        data = bytes(a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher))))
        return data.decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


def mask(plain: str) -> str:
    """给前端回显用的脱敏形式。

    早期实现是「首字符 + 星号 + 末字符」，对邮箱授权码、API Key 这类凭据
    泄露得有点多（首字符往往是 sk- / 字母，末字符又常用于肉眼比对）。
    现在只保留**末 4 位**用于辨认身份，其余一律用固定长度的星号遮掉，
    长度不随原文变化，顺带避免把密钥长度也暴露出去。
    """
    if not plain:
        return ""
    if len(plain) <= 4:
        return "*" * max(len(plain), 4)
    return "*" * 8 + plain[-4:]
