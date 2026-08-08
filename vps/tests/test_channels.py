from __future__ import annotations

from app.dependencies import current_channel_context

PASSWORD = "Valid!Pass9072"


def auth_headers(client, email: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_restricted_user_cannot_select_unlisted_channel(client, create_user, create_channel):
    allowed = create_channel(channel_key="allowed")
    denied = create_channel(channel_key="denied")
    create_user(email="member@example.com", accessible_channel_ids=[allowed.id])
    headers = auth_headers(client, "member@example.com")
    headers["X-Channel-Id"] = str(denied.id)

    response = client.get("/api/channels/current", headers=headers)

    assert response.status_code == 403


def test_null_access_list_allows_every_channel(client, create_user, create_channel):
    channel = create_channel(channel_key="open")
    create_user(email="member@example.com", accessible_channel_ids=None)
    headers = auth_headers(client, "member@example.com")
    headers["X-Channel-Id"] = str(channel.id)

    response = client.get("/api/channels/current", headers=headers)

    assert response.status_code == 200
    assert response.json()["id"] == channel.id


def test_unspecified_channel_uses_default(client, create_user, create_channel):
    default = create_channel(channel_key="default", is_default=True)
    create_user(email="member@example.com")

    response = client.get(
        "/api/channels/current",
        headers=auth_headers(client, "member@example.com"),
    )

    assert response.status_code == 200
    assert response.json()["id"] == default.id


def test_current_channel_context_is_reset_after_request(client, create_user, create_channel):
    default = create_channel(channel_key="default", is_default=True)
    create_user(email="member@example.com")

    response = client.get(
        "/api/channels/current",
        headers=auth_headers(client, "member@example.com"),
    )

    assert response.status_code == 200
    assert response.json()["id"] == default.id
    assert current_channel_context.get() is None


def test_unspecified_channel_without_default_is_forbidden(client, create_user, create_channel):
    create_channel(channel_key="not-default")
    create_user(email="member@example.com")

    response = client.get(
        "/api/channels/current",
        headers=auth_headers(client, "member@example.com"),
    )

    assert response.status_code == 403


def test_inactive_channel_is_not_found(client, create_user, create_channel):
    channel = create_channel(channel_key="inactive", is_active=False)
    create_user(email="member@example.com")
    headers = auth_headers(client, "member@example.com")
    headers["X-Channel-Id"] = str(channel.id)

    response = client.get("/api/channels/current", headers=headers)

    assert response.status_code == 404


def test_channel_responses_do_not_expose_credentials(client, create_user, create_channel):
    create_channel(
        channel_key="secret",
        is_default=True,
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        oauth_refresh_token="refresh-token",
    )
    create_user(email="member@example.com")
    headers = auth_headers(client, "member@example.com")

    listed = client.get("/api/channels", headers=headers)
    current = client.get("/api/channels/current", headers=headers)

    forbidden_fields = {
        "oauth_client_id",
        "oauth_client_secret",
        "oauth_client_secret_encrypted",
        "oauth_refresh_token",
        "oauth_refresh_token_encrypted",
        "credentials_updated_at",
    }
    assert listed.status_code == 200
    assert current.status_code == 200
    assert forbidden_fields.isdisjoint(listed.json()[0])
    assert forbidden_fields.isdisjoint(current.json())


def test_non_admin_cannot_create_channel(client, create_user):
    create_user(email="member@example.com")

    response = client.post(
        "/api/channels",
        json={"channel_key": "blocked", "name": "Blocked"},
        headers=auth_headers(client, "member@example.com"),
    )

    assert response.status_code == 403
