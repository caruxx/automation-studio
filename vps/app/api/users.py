"""Invitation and administrative user-management endpoints."""
from __future__ import annotations

import secrets
import re
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import get_current_user, require_admin
from ..models.db_models import User, UserInvitation, WorkerToken
from ..password_policy import assert_password_or_400
from ..rate_limit import check_rate_limit, client_ip
from ..models.db_models import WORKER_TOKEN_PREFIX_LEN
from ..security import hash_password


router = APIRouter(prefix="/api/users", tags=["users"])
worker_token_router = APIRouter(prefix="/api/worker-tokens", tags=["worker-tokens"])
ALLOWED_ROLES = {"admin", "user"}
INVITATION_TTL_HOURS = 168
_SIMPLE_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: Optional[str]
    role: str
    is_active: bool
    accessible_channel_ids: Optional[List[int]]
    created_at: datetime
    last_login_at: Optional[datetime]


class CreateInvitationRequest(BaseModel):
    email: str
    role: str = "user"


class InvitationResponse(BaseModel):
    id: int
    email: str
    role: str
    token: str
    expires_at: datetime


class AcceptInvitationRequest(BaseModel):
    token: str
    password: str
    name: Optional[str] = None


class CreateWorkerTokenRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str


class WorkerTokenCreatedResponse(BaseModel):
    id: int
    label: str
    token_prefix: str
    token: str


class WorkerTokenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    token_prefix: str
    last_seen_at: Optional[datetime]


def _resolve_invitation(db: Session, token: str) -> UserInvitation:
    invitation = db.query(UserInvitation).filter(UserInvitation.token == token).first()
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    if invitation.accepted_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation already used")
    if invitation.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation revoked")
    if invitation.expires_at < datetime.utcnow():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation expired")
    return invitation


@router.post("/invitations", response_model=InvitationResponse)
def create_invitation(
    payload: CreateInvitationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if payload.role not in ALLOWED_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")
    email = str(payload.email).lower().strip()
    if not _SIMPLE_EMAIL_RE.match(email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid email")
    if db.query(User).filter(User.email == email).first() is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    db.query(UserInvitation).filter(
        UserInvitation.email == email,
        UserInvitation.accepted_at.is_(None),
        UserInvitation.revoked_at.is_(None),
    ).update({UserInvitation.revoked_at: datetime.utcnow()})
    invitation = UserInvitation(
        email=email,
        role=payload.role,
        token=secrets.token_urlsafe(32),
        invited_by_user_id=current_user.id,
        expires_at=datetime.utcnow() + timedelta(hours=INVITATION_TTL_HOURS),
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)
    return invitation


@router.post("/invitations/accept", response_model=UserResponse, status_code=201)
def accept_invitation(
    payload: AcceptInvitationRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    check_rate_limit(
        f"invitation-accept:ip:{client_ip(request)}",
        max_attempts=20,
        window_seconds=3600,
    )
    invitation = _resolve_invitation(db, payload.token)
    assert_password_or_400(payload.password, email=invitation.email, name=payload.name)
    if db.query(User).filter(User.email == invitation.email).first() is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    now = datetime.utcnow()
    user = User(
        email=invitation.email,
        password_hash=hash_password(payload.password),
        password_changed_at=now,
        name=payload.name or None,
        role=invitation.role,
        is_active=True,
    )
    db.add(user)
    invitation.accepted_at = now
    db.commit()
    db.refresh(user)
    return user


@router.get("", response_model=List[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("/{user_id}/disable", response_model=UserResponse)
def disable_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An admin cannot disable their own account",
        )
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.is_active = False
    db.commit()
    db.refresh(user)
    return user


@worker_token_router.post("", response_model=WorkerTokenCreatedResponse, status_code=201)
def create_worker_token(
    payload: CreateWorkerTokenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    label = payload.label.strip()
    if not label or len(label) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Label must contain between 1 and 128 characters",
        )
    plain_token = secrets.token_urlsafe(32)
    worker_token = WorkerToken(
        user_id=current_user.id,
        label=label,
        token_hash=hash_password(plain_token),
        token_prefix=plain_token[:WORKER_TOKEN_PREFIX_LEN],
    )
    db.add(worker_token)
    db.commit()
    db.refresh(worker_token)
    return {
        "id": worker_token.id,
        "label": worker_token.label,
        "token_prefix": worker_token.token_prefix,
        "token": plain_token,
    }


@worker_token_router.get("", response_model=List[WorkerTokenResponse])
def list_worker_tokens(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(WorkerToken)
        .filter(WorkerToken.user_id == current_user.id)
        .order_by(WorkerToken.created_at.desc(), WorkerToken.id.desc())
        .all()
    )


@worker_token_router.post("/{token_id}/disable", response_model=WorkerTokenResponse)
def disable_worker_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    worker_token = (
        db.query(WorkerToken)
        .filter(
            WorkerToken.id == token_id,
            WorkerToken.user_id == current_user.id,
        )
        .first()
    )
    if worker_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker token not found")
    if worker_token.disabled_at is None:
        worker_token.disabled_at = datetime.utcnow()
        db.commit()
        db.refresh(worker_token)
    return worker_token
