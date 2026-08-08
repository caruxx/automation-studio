"""Login, logout, and session-check endpoints."""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import (
    TOKEN_COOKIE_NAME,
    get_current_user,
    get_current_user_for_auth_completion,
)
from ..models.db_models import User
from ..rate_limit import check_rate_limit, client_ip, reset_rate_limit
from ..security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    MFA_REQUIRED,
    create_access_token,
    generate_totp_secret,
    hash_password,
    verify_and_get_refreshed_hash,
    verify_password,
    verify_totp,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])
LOCKOUT_THRESHOLD = int(os.getenv("AUTH_LOCKOUT_THRESHOLD", "5"))
LOCKOUT_DURATION = timedelta(minutes=int(os.getenv("AUTH_LOCKOUT_MINUTES", "15")))
FAIL_COUNTER_RESET_AFTER = timedelta(hours=24)
AUTH_LOGIN_IP_MAX_ATTEMPTS = int(os.getenv("AUTH_LOGIN_IP_MAX_ATTEMPTS", "20"))
AUTH_LOGIN_IP_WINDOW_SECONDS = int(os.getenv("AUTH_LOGIN_IP_WINDOW_SECONDS", "300"))
AUTH_LOGIN_EMAIL_MAX_ATTEMPTS = int(os.getenv("AUTH_LOGIN_EMAIL_MAX_ATTEMPTS", "10"))
AUTH_LOGIN_EMAIL_WINDOW_SECONDS = int(os.getenv("AUTH_LOGIN_EMAIL_WINDOW_SECONDS", "900"))
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
COOKIE_SECURE = os.getenv(
    "COOKIE_SECURE",
    "true" if APP_ENV in {"production", "prod"} else "false",
).strip().lower() in {"1", "true", "yes", "on"}
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax").strip().lower()
_SIMPLE_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MFA_SETUP_TOKEN_EXPIRE_MINUTES = 10
TOTP_ISSUER = os.getenv("TOTP_ISSUER", "Automation Studio").strip() or "Automation Studio"
RECOVERY_CODE_COUNT = 8


class LoginRequest(BaseModel):
    email: str
    password: str
    totp_code: Optional[str] = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: Optional[str]
    role: str
    is_active: bool
    accessible_channel_ids: Optional[List[int]]


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class TotpSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TotpVerifyRequest(BaseModel):
    code: str


class TotpVerifyResponse(BaseModel):
    recovery_codes: List[str]


class TotpRecoverRequest(BaseModel):
    email: str
    password: str
    recovery_code: str


def _set_token_cookie(
    response: Response,
    token: str,
    *,
    expires_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES,
) -> None:
    response.set_cookie(
        key=TOKEN_COOKIE_NAME,
        value=token,
        max_age=expires_minutes * 60,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )


def _clear_token_cookie(response: Response) -> None:
    response.delete_cookie(
        key=TOKEN_COOKIE_NAME,
        path="/",
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
    )


def _record_failure(db: Session, user: User, now: datetime) -> bool:
    if user.last_failed_login_at is None or (
        now - user.last_failed_login_at >= FAIL_COUNTER_RESET_AFTER
    ):
        user.failed_login_count = 0
    user.failed_login_count = (user.failed_login_count or 0) + 1
    user.last_failed_login_at = now
    locked = user.failed_login_count >= LOCKOUT_THRESHOLD
    if locked:
        user.locked_until = now + LOCKOUT_DURATION
    db.commit()
    return locked


def _authentication_failed(db: Session, user: Optional[User], email_key: str) -> None:
    check_rate_limit(
        f"login:email:{email_key}",
        max_attempts=AUTH_LOGIN_EMAIL_MAX_ATTEMPTS,
        window_seconds=AUTH_LOGIN_EMAIL_WINDOW_SECONDS,
    )
    if user is not None and _record_failure(db, user, datetime.utcnow()):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account locked after repeated login failures",
            headers={"Retry-After": str(int(LOCKOUT_DURATION.total_seconds()))},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email, password, or TOTP code",
    )


def _reset_login_failures(user: User) -> None:
    user.failed_login_count = 0
    user.locked_until = None
    user.last_failed_login_at = None


def _record_successful_login(user: User, request: Request, now: datetime) -> None:
    _reset_login_failures(user)
    user.last_login_at = now
    user.last_login_ip = client_ip(request)
    user.last_login_ua = (request.headers.get("user-agent") or "")[:512]


def _build_totp_uri(user: User, secret: str) -> str:
    label = quote(f"{TOTP_ISSUER}:{user.email}", safe="")
    query = urlencode(
        {
            "secret": secret,
            "issuer": TOTP_ISSUER,
            "algorithm": "SHA1",
            "digits": 6,
            "period": 30,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def _generate_recovery_codes() -> list[str]:
    return [secrets.token_hex(5).upper() for _ in range(RECOVERY_CODE_COUNT)]


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    check_rate_limit(
        f"login:ip:{ip}",
        max_attempts=AUTH_LOGIN_IP_MAX_ATTEMPTS,
        window_seconds=AUTH_LOGIN_IP_WINDOW_SECONDS,
    )
    email_key = payload.email.lower().strip()
    if not _SIMPLE_EMAIL_RE.match(email_key):
        _authentication_failed(db, None, email_key)

    user = db.query(User).filter(User.email == email_key).first()
    now = datetime.utcnow()
    if user is not None and user.locked_until and user.locked_until > now:
        retry_after = max(1, int((user.locked_until - now).total_seconds()))
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account is temporarily locked",
            headers={"Retry-After": str(retry_after)},
        )

    valid = False
    refreshed_hash = None
    if user is not None and user.is_active:
        valid, refreshed_hash = verify_and_get_refreshed_hash(
            payload.password,
            user.password_hash,
        )
    if not valid:
        _authentication_failed(db, user, email_key)

    if user.totp_enabled:
        if not verify_totp(user.totp_secret or "", (payload.totp_code or "").strip()):
            _authentication_failed(db, user, email_key)
    elif MFA_REQUIRED:
        if refreshed_hash:
            user.password_hash = refreshed_hash
        _reset_login_failures(user)
        db.commit()
        reset_rate_limit(f"login:email:{email_key}")
        token = create_access_token(
            user.email,
            expires_minutes=MFA_SETUP_TOKEN_EXPIRE_MINUTES,
            auth_state="mfa_setup",
        )
        _set_token_cookie(
            response,
            token,
            expires_minutes=MFA_SETUP_TOKEN_EXPIRE_MINUTES,
        )
        response.headers["X-MFA-Setup-Required"] = "true"
        return LoginResponse(access_token=token, user=UserResponse.model_validate(user))

    if refreshed_hash:
        user.password_hash = refreshed_hash
    _record_successful_login(user, request, now)
    db.commit()
    db.refresh(user)
    reset_rate_limit(f"login:email:{email_key}")

    token = create_access_token(user.email, auth_state="full")
    _set_token_cookie(response, token)
    return LoginResponse(access_token=token, user=UserResponse.model_validate(user))


@router.post("/totp/setup", response_model=TotpSetupResponse)
def setup_totp(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_for_auth_completion),
):
    if current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TOTP is already enabled",
        )
    secret = generate_totp_secret()
    current_user.totp_secret = secret
    current_user.totp_recovery_codes = None
    db.commit()
    return TotpSetupResponse(
        secret=secret,
        otpauth_uri=_build_totp_uri(current_user, secret),
    )


@router.post("/totp/verify", response_model=TotpVerifyResponse)
def verify_totp_setup(
    payload: TotpVerifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_for_auth_completion),
):
    if current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TOTP is already enabled",
        )
    if not current_user.totp_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TOTP setup has not been started",
        )
    if not verify_totp(current_user.totp_secret, payload.code.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid TOTP code",
        )
    recovery_codes = _generate_recovery_codes()
    current_user.totp_enabled = True
    current_user.totp_recovery_codes = [hash_password(code) for code in recovery_codes]
    db.commit()
    return TotpVerifyResponse(recovery_codes=recovery_codes)


@router.post("/totp/recover", response_model=LoginResponse)
def recover_with_totp_code(
    payload: TotpRecoverRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    check_rate_limit(
        f"login:ip:{ip}",
        max_attempts=AUTH_LOGIN_IP_MAX_ATTEMPTS,
        window_seconds=AUTH_LOGIN_IP_WINDOW_SECONDS,
    )
    email_key = payload.email.lower().strip()
    if not _SIMPLE_EMAIL_RE.match(email_key):
        _authentication_failed(db, None, email_key)

    user = (
        db.query(User)
        .filter(User.email == email_key)
        .with_for_update()
        .first()
    )
    now = datetime.utcnow()
    if user is not None and user.locked_until and user.locked_until > now:
        retry_after = max(1, int((user.locked_until - now).total_seconds()))
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account is temporarily locked",
            headers={"Retry-After": str(retry_after)},
        )

    password_valid = False
    refreshed_hash = None
    if user is not None and user.is_active and user.totp_enabled:
        password_valid, refreshed_hash = verify_and_get_refreshed_hash(
            payload.password,
            user.password_hash,
        )

    recovery_code = payload.recovery_code.strip().upper()
    recovery_hashes = list(user.totp_recovery_codes or []) if user is not None else []
    matched_index = None
    if password_valid and recovery_code:
        matched_index = next(
            (
                index
                for index, recovery_hash in enumerate(recovery_hashes)
                if verify_password(recovery_code, recovery_hash)
            ),
            None,
        )
    if not password_valid or matched_index is None:
        _authentication_failed(db, user, email_key)

    recovery_hashes.pop(matched_index)
    user.totp_recovery_codes = recovery_hashes
    if refreshed_hash:
        user.password_hash = refreshed_hash
    _record_successful_login(user, request, now)
    db.commit()
    db.refresh(user)
    reset_rate_limit(f"login:email:{email_key}")

    token = create_access_token(user.email, auth_state="full")
    _set_token_cookie(response, token)
    return LoginResponse(access_token=token, user=UserResponse.model_validate(user))


@router.post("/logout")
def logout(response: Response):
    _clear_token_cookie(response)
    return {"success": True}


@router.get("/check", response_model=UserResponse)
def check(current_user: User = Depends(get_current_user)):
    return current_user
