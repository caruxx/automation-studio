"""Authentication dependencies shared by API routers."""
from contextvars import ContextVar
from typing import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import get_db
from .models.db_models import User, YouTubeChannel
from .security import MFA_REQUIRED, decode_token_claims


TOKEN_COOKIE_NAME = "as_studio_token"
CHANNEL_COOKIE_NAME = "as_studio_channel_id"
current_channel_context: ContextVar[YouTubeChannel | None] = ContextVar(
    "current_channel",
    default=None,
)


def _extract_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    return request.cookies.get(TOKEN_COOKIE_NAME, "") or ""


def _authenticated_user(
    request: Request,
    db: Session,
    *,
    allowed_states: set[str],
) -> User:
    token = _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = decode_token_claims(token)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = (
        db.query(User)
        .filter(User.email == claims["sub"], User.is_active.is_(True))
        .first()
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User does not exist or is disabled",
        )
    auth_state = str(claims.get("auth_state") or "full")
    if auth_state not in allowed_states:
        headers = {}
        if auth_state == "mfa_setup":
            headers["X-MFA-Setup-Required"] = "true"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Additional authentication setup is required",
            headers=headers,
        )
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = _authenticated_user(request, db, allowed_states={"full"})
    if MFA_REQUIRED and not user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="TOTP enrollment is required before using this API",
            headers={"X-MFA-Setup-Required": "true"},
        )
    return user


def get_current_user_for_auth_completion(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    return _authenticated_user(request, db, allowed_states={"full", "mfa_setup"})


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return user


def _requested_channel_id(request: Request) -> int | None:
    raw_value = request.headers.get("X-Channel-Id")
    if raw_value is None:
        raw_value = request.query_params.get("channel_id")
    if raw_value is None:
        raw_value = request.cookies.get(CHANNEL_COOKIE_NAME)
    if raw_value is None:
        return None
    if not raw_value.isascii() or not raw_value.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Channel id must be a positive integer",
        )
    channel_id = int(raw_value)
    if channel_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Channel id must be a positive integer",
        )
    return channel_id


def accessible_channel_id_set(user: User) -> set[int] | None:
    values = user.accessible_channel_ids
    if values is None:
        return None
    if not isinstance(values, list):
        return set()
    return {
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }


async def get_current_channel(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AsyncGenerator[YouTubeChannel, None]:
    channel_id = _requested_channel_id(request)
    if channel_id is None:
        channel = (
            db.query(YouTubeChannel)
            .filter(YouTubeChannel.is_default.is_(True))
            .order_by(YouTubeChannel.id)
            .first()
        )
        if channel is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No default channel is configured",
            )
    else:
        channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
        if channel is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Channel not found",
            )

    if not channel.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found",
        )

    allowed_ids = accessible_channel_id_set(current_user)
    if allowed_ids is not None and channel.id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Channel access denied",
        )

    token = current_channel_context.set(channel)
    try:
        yield channel
    finally:
        current_channel_context.reset(token)
