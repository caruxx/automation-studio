"""Job lifecycle queries shared by worker and monitoring endpoints."""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, time, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.db_models import Job


def reap_expired_leases(db: Session) -> list[Job]:
    now = datetime.utcnow()
    jobs = (
        db.query(Job)
        .filter(
            Job.state == "leased",
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at <= now,
        )
        .with_for_update()
        .all()
    )
    for job in jobs:
        job.leased_by_worker_token_id = None
        job.lease_expires_at = None
        if job.attempts >= job.max_attempts:
            job.state = "failed"
            job.error = job.error or "Maximum attempts reached after lease expiration"
            job.finished_at = now
        else:
            job.state = "queued"
    db.commit()
    return jobs


def overdue_jobs(db: Session, within_minutes: int = 30) -> list[Job]:
    threshold = datetime.utcnow() + timedelta(minutes=within_minutes)
    return (
        db.query(Job)
        .filter(
            Job.state == "queued",
            Job.deadline_at.is_not(None),
            Job.deadline_at <= threshold,
        )
        .order_by(Job.deadline_at.asc(), Job.id.asc())
        .all()
    )


def build_overdue_message(jobs: list[Job]) -> str:
    if not jobs:
        return "No queued jobs are approaching their deadline."
    lines = [f"{len(jobs)} queued job(s) are approaching their deadline:"]
    lines.extend(
        f"- job {job.id}: {job.job_type}, channel {job.channel_id}, deadline {job.deadline_at.isoformat()}"
        for job in jobs
    )
    return "\n".join(lines)


def quota_summary(db: Session, channel_id: int, date: date_type) -> int:
    start = datetime.combine(date, time.min)
    end = start + timedelta(days=1)
    value = (
        db.query(func.coalesce(func.sum(Job.quota_units), 0))
        .filter(
            Job.channel_id == channel_id,
            Job.state == "succeeded",
            Job.finished_at >= start,
            Job.finished_at < end,
        )
        .scalar()
    )
    return int(value or 0)
