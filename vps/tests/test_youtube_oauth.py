from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from app.models.db_models import OAuthState, YouTubeChannel
from app.services import youtube_credentials
from conftest import TestingSessionLocal


PASSWORD = "Valid!Pass9072"


def auth_headers(client, email: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def start_and_get_state(client, channel_id: int, headers: dict[str, str]) -> str:
    response = client.post(f"/api/channels/{channel_id}/oauth/start", headers=headers)
    assert response.status_code == 200
    query = parse_qs(urlparse(response.json()["authorization_url"]).query)
    return query["state"][0]


def test_start_denies_inaccessible_channel(client, create_user, create_channel):
    allowed = create_channel(channel_key="allowed")
    denied = create_channel(
        channel_key="denied",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
    )
    create_user(email="member@example.com", accessible_channel_ids=[allowed.id])

    response = client.post(
        f"/api/channels/{denied.id}/oauth/start",
        headers=auth_headers(client, "member@example.com"),
    )

    assert response.status_code == 403


def test_start_rejects_channel_without_client(client, create_user, create_channel):
    channel = create_channel(channel_key="missing-client")
    create_user(email="member@example.com", accessible_channel_ids=[channel.id])

    response = client.post(
        f"/api/channels/{channel.id}/oauth/start",
        headers=auth_headers(client, "member@example.com"),
    )

    assert response.status_code == 400


def test_callback_encrypts_refresh_token_and_consumes_state(
    client, create_user, create_channel, monkeypatch
):
    channel = create_channel(
        channel_key="oauth-success",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
    )
    create_user(email="member@example.com", accessible_channel_ids=[channel.id])
    state = start_and_get_state(
        client, channel.id, auth_headers(client, "member@example.com")
    )
    monkeypatch.setattr(
        "app.api.oauth.exchange_code",
        lambda *_args: {
            "refresh_token": "plain-refresh-token",
            "access_token": "access-token",
            "expires_in": 3600,
        },
    )

    response = client.get("/api/oauth/callback", params={"code": "code", "state": state})

    assert response.status_code == 200
    with TestingSessionLocal() as db:
        saved = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel.id).one()
        assert saved.oauth_refresh_token == "plain-refresh-token"
        assert saved.oauth_refresh_token_encrypted != "plain-refresh-token"
        assert "plain-refresh-token" not in saved.oauth_refresh_token_encrypted
        assert saved.credentials_updated_at is not None

    reused = client.get("/api/oauth/callback", params={"code": "code", "state": state})
    assert reused.status_code == 400


def test_callback_rejects_expired_and_unknown_state(
    client, create_user, create_channel, monkeypatch
):
    channel = create_channel(
        channel_key="expired",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
    )
    user = create_user(email="member@example.com")
    with TestingSessionLocal() as db:
        db.add(
            OAuthState(
                state="expired-state",
                channel_id=channel.id,
                user_id=user.id,
                redirect_uri="https://example.com/api/oauth/callback",
                expires_at=datetime.utcnow() - timedelta(seconds=1),
            )
        )
        db.commit()
    monkeypatch.setattr(
        "app.api.oauth.exchange_code",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not exchange")),
    )

    expired = client.get(
        "/api/oauth/callback", params={"code": "code", "state": "expired-state"}
    )
    unknown = client.get(
        "/api/oauth/callback", params={"code": "code", "state": "unknown-state"}
    )

    assert expired.status_code == 400
    assert unknown.status_code == 400


def test_callback_without_refresh_token_preserves_existing_value(
    client, create_user, create_channel, monkeypatch
):
    channel = create_channel(
        channel_key="preserve",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        oauth_refresh_token="existing-refresh-token",
    )
    create_user(email="member@example.com", accessible_channel_ids=[channel.id])
    state = start_and_get_state(
        client, channel.id, auth_headers(client, "member@example.com")
    )
    monkeypatch.setattr(
        "app.api.oauth.exchange_code",
        lambda *_args: {"refresh_token": None, "access_token": "access", "expires_in": 3600},
    )

    response = client.get("/api/oauth/callback", params={"code": "code", "state": state})

    assert response.status_code == 400
    with TestingSessionLocal() as db:
        saved = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel.id).one()
        assert saved.oauth_refresh_token == "existing-refresh-token"


def test_status_and_channel_responses_never_expose_oauth_values(
    client, create_user, create_channel
):
    channel = create_channel(
        channel_key="hidden",
        is_default=True,
        oauth_client_id="sensitive-client-id",
        oauth_client_secret="sensitive-client-secret",
        oauth_refresh_token="sensitive-refresh-token",
    )
    create_user(email="member@example.com")
    headers = auth_headers(client, "member@example.com")

    responses = [
        client.get(f"/api/channels/{channel.id}/oauth/status", headers=headers),
        client.get("/api/channels", headers=headers),
        client.get("/api/channels/current", headers=headers),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.text
        assert "sensitive-client-id" not in body
        assert "sensitive-client-secret" not in body
        assert "sensitive-refresh-token" not in body
        assert "client_secret" not in body
        assert "refresh_token" not in body


def test_get_access_token_caches_and_refreshes_near_expiry(create_channel, monkeypatch):
    channel_id = create_channel(
        channel_key="cache",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        oauth_refresh_token="refresh-token",
    ).id
    calls: list[int] = []

    def fake_refresh(*_args):
        calls.append(len(calls) + 1)
        return {"access_token": f"access-{len(calls)}", "expires_in": 120}

    clock = iter([1000.0, 1059.0, 1060.0])
    monkeypatch.setattr(youtube_credentials, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(youtube_credentials.time, "monotonic", lambda: next(clock))
    youtube_credentials.clear_access_token_cache()

    with TestingSessionLocal() as db:
        channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).one()
        first = youtube_credentials.get_access_token(db, channel)
        cached = youtube_credentials.get_access_token(db, channel)
        refreshed = youtube_credentials.get_access_token(db, channel)

    assert first == "access-1"
    assert cached == "access-1"
    assert refreshed == "access-2"
    assert len(calls) == 2
