"""Authentication dependencies shared by API routers."""
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import get_db
from .models.db_models import User
from .security import MFA_REQUIRED, decode_token_claims


TOKEN_COOKIE_NAME = "as_studio_token"


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
