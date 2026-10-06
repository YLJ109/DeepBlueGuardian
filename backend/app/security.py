"""鉴权与安全工具。

密码使用 PBKDF2-HMAC-SHA256 加盐散列；令牌使用 HMAC-SHA256 签名的
自包含 Token（与 JWT 结构兼容），不引入额外三方依赖。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .config import settings

_PBKDF2_ROUNDS = 200_000


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds))
        return hmac.compare_digest(calc.hex(), digest)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def create_token(payload: dict[str, Any], ttl_seconds: int | None = None) -> str:
    body = dict(payload)
    now = int(time.time())
    body.update({"iat": now, "exp": now + (ttl_seconds or settings.jwt_ttl_seconds)})
    header = {"alg": "HS256", "typ": "JWT"}
    segments = [
        _b64e(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64e(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    segments.append(_b64e(signature))
    return ".".join(segments)


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected = hmac.new(settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(signature_b64)):
            return None
        payload = json.loads(_b64d(payload_b64))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 设备接入签名
# ---------------------------------------------------------------------------
def device_signature(device_code: str, ts: int, nonce: str) -> str:
    msg = f"{device_code}|{ts}|{nonce}".encode("utf-8")
    return hmac.new(settings.device_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def verify_device_signature(device_code: str, ts: int, nonce: str, signature: str, max_skew: int = 300) -> bool:
    if abs(int(time.time()) - int(ts)) > max_skew:
        return False
    return hmac.compare_digest(device_signature(device_code, ts, nonce), signature)
