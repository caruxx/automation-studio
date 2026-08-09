"""YouTube OAuth start, callback, status, and revocation endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta
import os
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import accessible_channel_id_set, get_current_user, require_admin
from ..models.db_models import OAuthState, User, YouTubeChannel
from ..services.token_crypto import TokenEncryptionError
from ..services.youtube_credentials import clear_access_token_cache
from ..services.youtube_oauth import YouTubeOAuthError, build_authorization_url, exchange_code


router = APIRouter(tags=["youtube-oauth"])


class OAuthStatusResponse(BaseModel):
    has_credentials: bool
    credentials_updated_at: datetime | None


def _accessible_channel(db: Session, channel_id: int, user: User) -> YouTubeChannel:
    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if channel is None or not channel.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    allowed_ids = accessible_channel_id_set(user)
    if allowed_ids is not None and channel.id not in allowed_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Channel access denied")
    return channel


def _redirect_uri(request: Request) -> str:
    configured = os.getenv("YOUTUBE_OAUTH_REDIRECT_URI", "").strip()
    return configured or str(request.url_for("youtube_oauth_callback"))


def _oauth_done_redirect(error: str | None = None) -> RedirectResponse:
    location = "/oauth-done.html"
    if error:
        location = f"{location}?error={quote(error, safe='')}"
    return RedirectResponse(location, status_code=status.HTTP_302_FOUND)


@router.post("/api/channels/{channel_id}/oauth/start")
def start_oauth(
    channel_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    channel = _accessible_channel(db, channel_id, current_user)
    if not channel.oauth_client_id or not channel.oauth_client_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth client is not configured",
        )
    now = datetime.utcnow()
    state_value = secrets.token_urlsafe(32)
    redirect_uri = _redirect_uri(request)
    oauth_state = OAuthState(
        state=state_value,
        channel_id=channel.id,
        user_id=current_user.id,
        redirect_uri=redirect_uri,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    db.add(oauth_state)
    db.commit()
    return {
        "authorization_url": build_authorization_url(
            channel.oauth_client_id,
            redirect_uri,
            state_value,
        )
    }


@router.get("/api/oauth/callback", name="youtube_oauth_callback")
def oauth_callback(
    code: str = "",
    state: str = "",
    db: Session = Depends(get_db),
):
    if not code or not state:
        return _oauth_done_redirect("Invalid OAuth callback")
    now = datetime.utcnow()
    oauth_state = (
        db.query(OAuthState)
        .filter(OAuthState.state == state)
        .with_for_update()
        .first()
    )
    if (
        oauth_state is None
        or oauth_state.consumed_at is not None
        or oauth_state.expires_at <= now
    ):
        return _oauth_done_redirect("Invalid OAuth state")

    oauth_state.consumed_at = now
    db.commit()
    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == oauth_state.channel_id).first()
    if (
        channel is None
        or not channel.oauth_client_id
        or not channel.oauth_client_secret_encrypted
    ):
        return _oauth_done_redirect("OAuth client unavailable")

    try:
        result = exchange_code(
            channel.oauth_client_id,
            channel.oauth_client_secret,
            code,
            oauth_state.redirect_uri,
        )
    except (TokenEncryptionError, YouTubeOAuthError):
        return _oauth_done_redirect("OAuth code exchange failed")
    refresh_token = result.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return _oauth_done_redirect("OAuth response did not include a refresh token")

    channel.oauth_refresh_token = refresh_token
    channel.credentials_updated_at = datetime.utcnow()
    db.commit()
    clear_access_token_cache()
    return _oauth_done_redirect()


@router.delete("/api/channels/{channel_id}/oauth")
def delete_oauth(
    channel_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    channel.oauth_refresh_token = None
    channel.credentials_updated_at = None
    db.commit()
    clear_access_token_cache()
    return {"success": True}


@router.get(
    "/api/channels/{channel_id}/oauth/status",
    response_model=OAuthStatusResponse,
)
def oauth_status(
    channel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    channel = _accessible_channel(db, channel_id, current_user)
    return OAuthStatusResponse(
        has_credentials=bool(channel.oauth_refresh_token_encrypted),
        credentials_updated_at=channel.credentials_updated_at,
    )
