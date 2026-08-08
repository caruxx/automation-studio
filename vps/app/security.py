"""Password hashing, JWT issuance, and TOTP verification."""
from __future__ import annotations

import os
import re
import base64
import hashlib
import hmac
import struct
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import jwt
from jwt import InvalidTokenError
from passlib.context import CryptContext


SECRET_KEY = os.getenv("SECRET_KEY", "dev-dummy-secret-please-change")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
APP_ENV = os.getenv("APP_ENV", "development").lower()
MFA_REQUIRED = os.getenv(
    "MFA_REQUIRED",
    "true" if APP_ENV in {"production", "prod"} else "false",
).strip().lower() in {"1", "true", "yes", "on"}
PASSWORD_MAX_AGE_DAYS = int(os.getenv("PASSWORD_MAX_AGE_DAYS", "365"))
PASSWORD_MIN_AGE_HOURS = int(os.getenv("PASSWORD_MIN_AGE_HOURS", "24"))
BCRYPT_ROUNDS = int(os.getenv("BCRYPT_ROUNDS", "12"))
BCRYPT_REHASH_ON_LOGIN = os.getenv(
    "BCRYPT_REHASH_ON_LOGIN",
    "true" if APP_ENV in {"production", "prod"} else "false",
).strip().lower() in {"1", "true", "yes", "on"}

if APP_ENV in {"production", "prod"} and (
    SECRET_KEY == "dev-dummy-secret-please-change" or len(SECRET_KEY) < 32
):
    raise RuntimeError("Production requires SECRET_KEY with at least 32 characters")

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=BCRYPT_ROUNDS,
)
_BCRYPT_HASH_RE = re.compile(r"^\$(2[abyx])\$(\d{2})\$")


def _is_bcrypt_hash(hashed: str) -> bool:
    return bool(_BCRYPT_HASH_RE.match(hashed or ""))


def _parse_bcrypt_rounds(hashed: str) -> Optional[int]:
    match = _BCRYPT_HASH_RE.match(hashed or "")
    if not match:
        return None
    try:
        return int(match.group(2))
    except (TypeError, ValueError):
        return None


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    if _is_bcrypt_hash(hashed):
        try:
            return bool(bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8")))
        except Exception:
            return False
    try:
        return bool(pwd_context.verify(plain, hashed))
    except Exception:
        return False


def verify_and_get_refreshed_hash(plain: str, hashed: str) -> tuple[bool, Optional[str]]:
    valid = verify_password(plain, hashed)
    if not valid or not BCRYPT_REHASH_ON_LOGIN:
        return valid, None
    rounds = _parse_bcrypt_rounds(hashed)
    if rounds is not None and rounds < BCRYPT_ROUNDS:
        return True, hash_password(plain)
    return True, None


PASSWORD_HISTORY_KEEP = 10


def is_password_reused(plain: str, recent_hashes: list[str]) -> bool:
    for password_hash in recent_hashes:
        if not password_hash:
            continue
        try:
            if pwd_context.verify(plain, password_hash):
                return True
        except Exception:
            continue
    return False


def create_access_token(
    subject: str,
    expires_minutes: Optional[int] = None,
    *,
    auth_state: Optional[str] = None,
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or ACCESS_TOKEN_EXPIRE_MINUTES
    )
    claims: dict[str, Any] = {"sub": subject, "exp": expire}
    if auth_state is not None:
        claims["auth_state"] = auth_state
    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)


def decode_token_claims(token: str) -> Optional[dict[str, Any]]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if not isinstance(payload.get("sub"), str):
            return None
        return payload
    except InvalidTokenError:
        return None


def decode_token(token: str) -> Optional[str]:
    claims = decode_token_claims(token)
    return claims.get("sub") if claims else None


def password_is_expired(
    changed_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> bool:
    if changed_at is None:
        return True
    current = now or datetime.utcnow()
    if changed_at.tzinfo is not None:
        changed = changed_at.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        changed = changed_at
    return current - changed >= timedelta(days=PASSWORD_MAX_AGE_DAYS)


def password_change_too_recent(
    changed_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> bool:
    if changed_at is None:
        return False
    current = now or datetime.utcnow()
    if changed_at.tzinfo is not None:
        changed = changed_at.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        changed = changed_at
    return current - changed < timedelta(hours=PASSWORD_MIN_AGE_HOURS)


def generate_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def generate_totp(secret: str, *, at_time: float | None = None) -> str:
    normalized = secret.strip().replace(" ", "").upper()
    padded = normalized + "=" * (-len(normalized) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = int((at_time if at_time is not None else time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def verify_totp(secret: str, code: str, *, at_time: float | None = None) -> bool:
    if not secret or not code or not re.fullmatch(r"\d{6}", code):
        return False
    try:
        current = at_time if at_time is not None else time.time()
        return any(
            hmac.compare_digest(generate_totp(secret, at_time=current + offset * 30), code)
            for offset in (-1, 0, 1)
        )
    except Exception:
        return False
