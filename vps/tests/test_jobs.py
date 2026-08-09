from __future__ import annotations

from datetime import datetime, timedelta

from app.models.db_models import Job, WorkerToken
from app.services.job_service import quota_summary, reap_expired_leases
from conftest import TestingSessionLocal


PASSWORD = "Valid!Pass9072"


def user_headers(client, email: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def issue_worker(client, email: str, label: str = "test-worker") -> tuple[str, int]:
    response = client.post(
        "/api/worker-tokens",
        json={"label": label},
        headers=user_headers(client, email),
    )
    assert response.status_code == 201
    return response.json()["token"], response.json()["id"]


def worker_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def add_job(channel_id: int, *, max_attempts: int = 3, **values) -> int:
    with TestingSessionLocal() as db:
        job = Job(
            channel_id=channel_id,
            job_type=values.pop("job_type", "render"),
            payload=values.pop("payload", {}),
            max_attempts=max_attempts,
            **values,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id


def lease(client, token: str, **values):
    payload = {"job_types": ["render"], "lease_seconds": 300, "max_jobs": 1}
    payload.update(values)
    return client.post(
        "/api/worker/jobs/lease",
        json=payload,
        headers=worker_headers(token),
    )


def test_same_job_cannot_be_leased_twice(client, create_user, create_channel):
    channel = create_channel(channel_key="double-lease")
    create_user(email="worker@example.com", accessible_channel_ids=[channel.id])
    token, _ = issue_worker(client, "worker@example.com")
    job_id = add_job(channel.id)

    first = lease(client, token)
    second = lease(client, token)

    assert first.status_code == 200
    assert [job["id"] for job in first.json()] == [job_id]
    assert second.status_code == 200
    assert second.json() == []


def test_expired_lease_is_requeued(create_user, create_channel):
    channel = create_channel(channel_key="expired")
    user = create_user(email="worker@example.com", accessible_channel_ids=[channel.id])
    with TestingSessionLocal() as db:
        worker = WorkerToken(
            user_id=user.id,
            label="worker",
            token_hash="not-used",
            token_prefix="not-used",
        )
        db.add(worker)
        db.flush()
        job = Job(
            channel_id=channel.id,
            job_type="render",
            payload={},
            state="leased",
            leased_by_worker_token_id=worker.id,
            lease_expires_at=datetime.utcnow() - timedelta(seconds=1),
            attempts=1,
            max_attempts=3,
        )
        db.add(job)
        db.commit()
        job_id = job.id

        reap_expired_leases(db)
        refreshed = db.get(Job, job_id)
        assert refreshed.state == "queued"
        assert refreshed.leased_by_worker_token_id is None
        assert refreshed.lease_expires_at is None


def test_other_worker_cannot_complete_lease(client, create_user, create_channel):
    channel = create_channel(channel_key="ownership")
    create_user(email="one@example.com", accessible_channel_ids=[channel.id])
    create_user(email="two@example.com", accessible_channel_ids=[channel.id])
    token_one, _ = issue_worker(client, "one@example.com", "one")
    token_two, _ = issue_worker(client, "two@example.com", "two")
    job_id = add_job(channel.id)
    assert lease(client, token_one).status_code == 200

    response = client.post(
        f"/api/worker/jobs/{job_id}/complete",
        json={"result": {}, "quota_units": 0},
        headers=worker_headers(token_two),
    )

    assert response.status_code == 409


def test_retryable_failure_requeues_then_fails_at_max_attempts(
    client, create_user, create_channel
):
    channel = create_channel(channel_key="retry")
    create_user(email="worker@example.com", accessible_channel_ids=[channel.id])
    token, _ = issue_worker(client, "worker@example.com")
    job_id = add_job(channel.id, max_attempts=2)

    assert lease(client, token).status_code == 200
    first = client.post(
        f"/api/worker/jobs/{job_id}/fail",
        json={"error": "temporary", "retryable": True},
        headers=worker_headers(token),
    )
    assert first.status_code == 200
    assert first.json()["state"] == "queued"

    assert lease(client, token).status_code == 200
    second = client.post(
        f"/api/worker/jobs/{job_id}/fail",
        json={"error": "temporary again", "retryable": True},
        headers=worker_headers(token),
    )
    assert second.status_code == 200
    assert second.json()["state"] == "failed"


def test_disabled_worker_token_is_unauthorized(client, create_user, create_channel):
    channel = create_channel(channel_key="disabled")
    create_user(email="worker@example.com", accessible_channel_ids=[channel.id])
    token, token_id = issue_worker(client, "worker@example.com")
    disabled = client.post(
        f"/api/worker-tokens/{token_id}/disable",
        headers=user_headers(client, "worker@example.com"),
    )
    assert disabled.status_code == 200

    response = lease(client, token)

    assert response.status_code == 401


def test_worker_cannot_lease_inaccessible_channel(client, create_user, create_channel):
    allowed = create_channel(channel_key="allowed")
    denied = create_channel(channel_key="denied")
    create_user(email="worker@example.com", accessible_channel_ids=[allowed.id])
    token, _ = issue_worker(client, "worker@example.com")
    add_job(denied.id)

    response = lease(client, token)

    assert response.status_code == 200
    assert response.json() == []


def test_worker_credentials_only_return_access_token(
    client, create_user, create_channel, monkeypatch
):
    channel = create_channel(channel_key="credentials")
    create_user(email="worker@example.com", accessible_channel_ids=[channel.id])
    token, _ = issue_worker(client, "worker@example.com")
    monkeypatch.setattr("app.api.worker.get_access_token", lambda db, channel: "access-only")

    response = client.get(
        f"/api/worker/channels/{channel.id}/credentials",
        headers=worker_headers(token),
    )

    assert response.status_code == 200
    assert response.json() == {"access_token": "access-only"}
    assert "refresh_token" not in response.text
    assert "client_secret" not in response.text


def test_plain_worker_token_is_not_stored(client, create_user):
    create_user(email="worker@example.com")
    token, token_id = issue_worker(client, "worker@example.com")

    with TestingSessionLocal() as db:
        stored = db.get(WorkerToken, token_id)
        assert stored.token_hash != token
        assert token not in stored.token_hash
        assert stored.token_prefix == token[:8]
        assert len(stored.token_prefix) == 8


def test_quota_summary_totals_succeeded_jobs(create_channel):
    channel = create_channel(channel_key="quota")
    other = create_channel(channel_key="other-quota")
    now = datetime.utcnow()
    add_job(
        channel.id,
        state="succeeded",
        quota_units=7,
        finished_at=now,
    )
    add_job(
        channel.id,
        state="succeeded",
        quota_units=5,
        finished_at=now,
    )
    add_job(channel.id, state="failed", quota_units=100, finished_at=now)
    add_job(other.id, state="succeeded", quota_units=100, finished_at=now)

    with TestingSessionLocal() as db:
        assert quota_summary(db, channel.id, now.date()) == 12
