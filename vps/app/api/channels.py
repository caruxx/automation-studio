"""Channel management and per-request channel resolution endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import (
    accessible_channel_id_set,
    get_current_channel,
    get_current_user,
    require_admin,
)
from ..models.db_models import User, YouTubeChannel


router = APIRouter(prefix="/api/channels", tags=["channels"])


class ChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_key: str
    name: str
    handle: Optional[str]
    youtube_channel_id: Optional[str]
    prefix: Optional[str]
    folder_rel: Optional[str]
    is_active: bool
    is_default: bool
    note: Optional[str]
    created_at: datetime
    updated_at: datetime


class CreateChannelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    channel_key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    handle: Optional[str] = Field(default=None, max_length=128)
    youtube_channel_id: Optional[str] = Field(default=None, max_length=64)
    prefix: Optional[str] = Field(default=None, max_length=32)
    folder_rel: Optional[str] = None
    is_active: bool = True
    is_default: bool = False
    note: Optional[str] = None


class UpdateChannelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    channel_key: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    handle: Optional[str] = Field(default=None, max_length=128)
    youtube_channel_id: Optional[str] = Field(default=None, max_length=64)
    prefix: Optional[str] = Field(default=None, max_length=32)
    folder_rel: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    note: Optional[str] = None


def _get_channel_or_404(db: Session, channel_id: int) -> YouTubeChannel:
    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return channel


def _clear_other_defaults(db: Session, channel_id: int | None = None) -> None:
    channels = db.query(YouTubeChannel).with_for_update().all()
    for channel in channels:
        if channel_id is None or channel.id != channel_id:
            channel.is_default = False


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Channel key already exists",
        ) from None


@router.get("", response_model=List[ChannelResponse])
def list_channels(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(YouTubeChannel).filter(YouTubeChannel.is_active.is_(True))
    allowed_ids = accessible_channel_id_set(current_user)
    if allowed_ids is not None:
        query = query.filter(YouTubeChannel.id.in_(allowed_ids))
    return query.order_by(YouTubeChannel.name, YouTubeChannel.id).all()


@router.get("/current", response_model=ChannelResponse)
def current_channel(channel: YouTubeChannel = Depends(get_current_channel)):
    return channel


@router.post("", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
def create_channel(
    payload: CreateChannelRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if payload.is_default:
        _clear_other_defaults(db)
    channel = YouTubeChannel(**payload.model_dump())
    db.add(channel)
    _commit_or_conflict(db)
    db.refresh(channel)
    return channel


@router.patch("/{channel_id}", response_model=ChannelResponse)
def update_channel(
    channel_id: int,
    payload: UpdateChannelRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    channel = _get_channel_or_404(db, channel_id)
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("is_default") is True:
        _clear_other_defaults(db, channel.id)
    for field_name, value in updates.items():
        setattr(channel, field_name, value)
    _commit_or_conflict(db)
    db.refresh(channel)
    return channel


@router.post("/{channel_id}/default", response_model=ChannelResponse)
def set_default_channel(
    channel_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    channel = _get_channel_or_404(db, channel_id)
    _clear_other_defaults(db, channel.id)
    channel.is_default = True
    db.commit()
    db.refresh(channel)
    return channel
