from __future__ import annotations

import os
import subprocess
import sys

import app.api.auth as auth_api
import app.dependencies as auth_dependencies
from app.api.auth import LOCKOUT_THRESHOLD
from app.security import decode_token_claims, generate_totp


PASSWORD = "Valid!Pass9072"


def login(client, email: str, password: str = PASSWORD, totp_code: str | None = None):
    payload = {"email": email, "password": password}
    if totp_code is not None:
        payload["totp_code"] = totp_code
    return client.post("/api/auth/login", json=payload)


def test_login_success_with_totp_and_cookie(client, create_user):
    secret = "JBSWY3DPEHPK3PXP"
    create_user(email="member@example.com", totp_secret=secret)

    response = login(client, "member@example.com", totp_code=generate_totp(secret))

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "member@example.com"
    assert "as_studio_token=" in response.headers["set-cookie"]
    check = client.get("/api/auth/check")
    assert check.status_code == 200
    assert check.json()["email"] == "member@example.com"


def test_login_failure_uses_generic_error(client, create_user):
    create_user(email="member@example.com")

    response = login(client, "member@example.com", password="Wrong!Pass9072")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email, password, or TOTP code"


def test_login_lockout_after_repeated_failures(client, create_user):
    create_user(email="locked@example.com")

    responses = [
        login(client, "locked@example.com", password="Wrong!Pass9072")
        for _ in range(LOCKOUT_THRESHOLD)
    ]

    assert all(response.status_code == 401 for response in responses[:-1])
    assert responses[-1].status_code == 423
    assert "Retry-After" in responses[-1].headers
    assert login(client, "locked@example.com").status_code == 423


def test_admin_invitation_acceptance_flow(client, create_user):
    create_user(email="admin@example.com", role="admin")
    admin_login = login(client, "admin@example.com")
    token = admin_login.json()["access_token"]

    invitation = client.post(
        "/api/users/invitations",
        json={"email": "new-user@example.com", "role": "user"},
        headers={"Authorization": f"Bearer {token}"},
    )
    accepted = client.post(
        "/api/users/invitations/accept",
        json={
            "token": invitation.json()["token"],
            "name": "New User",
            "password": "Another!Pass9072",
        },
    )

    assert invitation.status_code == 200
    assert accepted.status_code == 201
    assert accepted.json()["email"] == "new-user@example.com"
    assert login(client, "new-user@example.com", "Another!Pass9072").status_code == 200


def test_management_api_requires_admin_and_can_disable(client, create_user):
    admin = create_user(email="admin@example.com", role="admin")
    member = create_user(email="member@example.com")
    member_token = login(client, "member@example.com").json()["access_token"]
    forbidden = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {member_token}"},
    )

    admin_token = login(client, "admin@example.com").json()["access_token"]
    listed = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    disabled = client.post(
        f"/api/users/{member.id}/disable",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert admin.id != member.id
    assert forbidden.status_code == 403
    assert listed.status_code == 200
    assert len(listed.json()) == 2
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    assert login(client, "member@example.com").status_code == 401


def test_production_rejects_short_secret_key():
    environment = os.environ.copy()
    environment.update({"APP_ENV": "production", "SECRET_KEY": "too-short"})
    result = subprocess.run(
        [sys.executable, "-c", "import app.security"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "at least 32 characters" in result.stderr


def test_mfa_setup_token_cannot_access_users_api(client, create_user, monkeypatch):
    monkeypatch.setattr(auth_api, "MFA_REQUIRED", True)
    monkeypatch.setattr(auth_dependencies, "MFA_REQUIRED", True)
    create_user(email="setup@example.com")

    response = login(client, "setup@example.com")
    token = response.json()["access_token"]
    forbidden = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["X-MFA-Setup-Required"] == "true"
    assert decode_token_claims(token)["auth_state"] == "mfa_setup"
    assert forbidden.status_code == 403
    assert forbidden.headers["X-MFA-Setup-Required"] == "true"


def test_totp_setup_and_verify_allows_full_login(client, create_user, monkeypatch):
    monkeypatch.setattr(auth_api, "MFA_REQUIRED", True)
    monkeypatch.setattr(auth_dependencies, "MFA_REQUIRED", True)
    create_user(email="setup@example.com")
    setup_token = login(client, "setup@example.com").json()["access_token"]
    headers = {"Authorization": f"Bearer {setup_token}"}

    setup = client.post("/api/auth/totp/setup", headers=headers)
    secret = setup.json()["secret"]
    verified = client.post(
        "/api/auth/totp/verify",
        json={"code": generate_totp(secret)},
        headers=headers,
    )
    full_login = login(
        client,
        "setup@example.com",
        totp_code=generate_totp(secret),
    )

    assert setup.status_code == 200
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert verified.status_code == 200
    assert len(verified.json()["recovery_codes"]) == 8
    assert full_login.status_code == 200
    assert decode_token_claims(full_login.json()["access_token"])["auth_state"] == "full"


def test_recovery_code_can_only_be_used_once(client, create_user, monkeypatch):
    monkeypatch.setattr(auth_api, "MFA_REQUIRED", True)
    monkeypatch.setattr(auth_dependencies, "MFA_REQUIRED", True)
    create_user(email="recovery@example.com")
    setup_token = login(client, "recovery@example.com").json()["access_token"]
    headers = {"Authorization": f"Bearer {setup_token}"}
    setup = client.post("/api/auth/totp/setup", headers=headers)
    verified = client.post(
        "/api/auth/totp/verify",
        json={"code": generate_totp(setup.json()["secret"])},
        headers=headers,
    )
    recovery_code = verified.json()["recovery_codes"][0]
    payload = {
        "email": "recovery@example.com",
        "password": PASSWORD,
        "recovery_code": recovery_code,
    }

    first = client.post("/api/auth/totp/recover", json=payload)
    second = client.post("/api/auth/totp/recover", json=payload)

    assert first.status_code == 200
    assert second.status_code == 401
