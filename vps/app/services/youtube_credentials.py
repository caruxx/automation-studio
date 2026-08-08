"""Access-token retrieval backed by encrypted per-channel credentials."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import threading
import time

from sqlalchemy.orm import Session

from ..models.db_models import YouTubeChannel
from .token_crypto import TokenEncryptionError
from .youtube_oauth import YouTubeOAuthError, refresh_access_token


class YouTubeCredentialsError(RuntimeError):
    """Credentials are unavailable or could not produce an access token."""


@dataclass(frozen=True)
class _CachedAccessToken:
    access_token: str
    refresh_at: float
    credential_version: datetime | None


_cache: dict[int, _CachedAccessToken] = {}
_cache_lock = threading.Lock()


def clear_access_token_cache() -> None:
    with _cache_lock:
        _cache.clear()


def get_access_token(db: Session, channel: YouTubeChannel) -> str:
    del db
    if (
        not channel.oauth_client_id
        or not channel.oauth_client_secret_encrypted
        or not channel.oauth_refresh_token_encrypted
    ):
        raise YouTubeCredentialsError("youtube_credentials_missing")

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(channel.id)
        if (
            cached is not None
            and cached.credential_version == channel.credentials_updated_at
            and now < cached.refresh_at
        ):
            return cached.access_token

        try:
            client_secret = channel.oauth_client_secret
            refresh_token = channel.oauth_refresh_token
            if (
                not isinstance(client_secret, str)
                or not client_secret
                or not isinstance(refresh_token, str)
                or not refresh_token
            ):
                raise YouTubeCredentialsError("youtube_credentials_invalid")
            result = refresh_access_token(
                channel.oauth_client_id,
                client_secret,
                refresh_token,
            )
            expires_in = int(result.get("expires_in") or 0)
        except (TypeError, ValueError, TokenEncryptionError, YouTubeOAuthError):
            raise YouTubeCredentialsError("youtube_access_token_refresh_failed") from None
        if expires_in <= 0:
            raise YouTubeCredentialsError("youtube_credentials_invalid")

        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise YouTubeCredentialsError("youtube_access_token_refresh_failed")
        _cache[channel.id] = _CachedAccessToken(
            access_token=token,
            refresh_at=now + max(0, expires_in - 60),
            credential_version=channel.credentials_updated_at,
        )
        return token
