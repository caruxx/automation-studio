"""Google OAuth helpers for YouTube credentials."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx


AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)


class YouTubeOAuthError(RuntimeError):
    """OAuth failed without exposing credentials or token response data."""


def build_authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(YOUTUBE_SCOPES),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
    )
    return f"{AUTHORIZATION_ENDPOINT}?{query}"


def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    """The single replaceable boundary for OAuth network requests."""
    try:
        response = httpx.post(TOKEN_ENDPOINT, data=payload, timeout=15.0)
        response.raise_for_status()
        result = response.json()
    except Exception:
        raise YouTubeOAuthError("youtube_oauth_request_failed") from None
    if not isinstance(result, dict):
        raise YouTubeOAuthError("youtube_oauth_response_invalid")
    return result


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    result = _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    if not isinstance(result.get("access_token"), str):
        raise YouTubeOAuthError("youtube_oauth_response_invalid")
    return {
        "refresh_token": result.get("refresh_token"),
        "access_token": result["access_token"],
        "expires_in": result.get("expires_in"),
    }


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    result = _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )
    if not isinstance(result.get("access_token"), str):
        raise YouTubeOAuthError("youtube_oauth_response_invalid")
    return {
        "access_token": result["access_token"],
        "expires_in": result.get("expires_in"),
    }
