"""Pull-based job API used by authenticated Mac workers."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import accessible_channel_id_set, get_current_worker
from ..models.db_models import Job, WorkerToken, YouTubeChannel
from ..services.youtube_credentials import YouTubeCredentialsError, get_access_token


router = APIRouter(prefix="/api/worker", tags=["worker"])


class LeaseRequest(BaseModel):
    job_types: list[str] = Field(min_length=1, max_length=32)
    lease_seconds: int = Field(default=300, ge=30, le=86400)
    max_jobs: int = Field(default=1, ge=1, le=100)


class CompleteRequest(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)
    quota_units: int = Field(default=0, ge=0)


class FailRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    error: str = Field(min_length=1, max_length=10000)
    retryable: bool


class HeartbeatRequest(BaseModel):
    lease_seconds: int = Field(default=300, ge=30, le=86400)


class WorkerJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    job_type: str
    payload: dict[str, Any]
    state: str
    priority: int
    deadline_at: datetime | None
    lease_expires_at: datetime | None
    attempts: int
    max_attempts: int
    result: dict[str, Any] | None
    error: str | None
    quota_units: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _worker_allowed_ids(worker: WorkerToken) -> set[int] | None:
    if worker.user is None or not worker.user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Worker owner is unavailable",
        )
    return accessible_channel_id_set(worker.user)


def _owned_active_lease(db: Session, worker_id: int, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id).with_for_update().first()
    if (
        job is None
        or job.state != "leased"
        or job.leased_by_worker_token_id != worker_id
        or job.lease_expires_at is None
        or job.lease_expires_at <= datetime.utcnow()
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job is not actively leased by this worker",
        )
    return job


def _assert_worker_job_access(worker: WorkerToken, job: Job) -> None:
    allowed_ids = _worker_allowed_ids(worker)
    if allowed_ids is not None and job.channel_id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Channel access denied",
        )


@router.post("/jobs/lease", response_model=list[WorkerJobResponse])
def lease_jobs(
    payload: LeaseRequest,
    db: Session = Depends(get_db),
    worker: WorkerToken = Depends(get_current_worker),
):
    worker_id = worker.id
    allowed_ids = _worker_allowed_ids(worker)
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        db.commit()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")

    now = datetime.utcnow()
    eligible_state = or_(
        Job.state == "queued",
        (Job.state == "leased")
        & Job.lease_expires_at.is_not(None)
        & (Job.lease_expires_at <= now),
    )
    query = (
        db.query(Job)
        .join(YouTubeChannel, Job.channel_id == YouTubeChannel.id)
        .filter(
            eligible_state,
            Job.job_type.in_(set(payload.job_types)),
            Job.attempts < Job.max_attempts,
            YouTubeChannel.is_active.is_(True),
        )
    )
    if allowed_ids is not None:
        query = query.filter(Job.channel_id.in_(allowed_ids))
    query = query.order_by(
        Job.priority.desc(),
        case((Job.deadline_at.is_(None), 1), else_=0).asc(),
        Job.deadline_at.asc(),
        Job.id.asc(),
    ).limit(payload.max_jobs)
    if dialect == "postgresql":
        query = query.with_for_update(skip_locked=True, of=Job)

    jobs = query.all()
    lease_expires_at = now + timedelta(seconds=payload.lease_seconds)
    for job in jobs:
        job.state = "leased"
        job.leased_by_worker_token_id = worker_id
        job.lease_expires_at = lease_expires_at
        job.attempts += 1
        if job.started_at is None:
            job.started_at = now
        job.finished_at = None
    db.commit()
    return jobs


@router.post("/jobs/{job_id}/complete", response_model=WorkerJobResponse)
def complete_job(
    job_id: int,
    payload: CompleteRequest,
    db: Session = Depends(get_db),
    worker: WorkerToken = Depends(get_current_worker),
):
    job = _owned_active_lease(db, worker.id, job_id)
    _assert_worker_job_access(worker, job)
    job.state = "succeeded"
    job.result = payload.result
    job.error = None
    job.quota_units = payload.quota_units
    job.lease_expires_at = None
    job.finished_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/fail", response_model=WorkerJobResponse)
def fail_job(
    job_id: int,
    payload: FailRequest,
    db: Session = Depends(get_db),
    worker: WorkerToken = Depends(get_current_worker),
):
    job = _owned_active_lease(db, worker.id, job_id)
    _assert_worker_job_access(worker, job)
    now = datetime.utcnow()
    job.error = payload.error
    job.lease_expires_at = None
    if payload.retryable and job.attempts < job.max_attempts:
        job.state = "queued"
        job.leased_by_worker_token_id = None
        job.finished_at = None
    else:
        job.state = "failed"
        job.finished_at = now
    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/heartbeat", response_model=WorkerJobResponse)
def heartbeat_job(
    job_id: int,
    payload: HeartbeatRequest,
    db: Session = Depends(get_db),
    worker: WorkerToken = Depends(get_current_worker),
):
    job = _owned_active_lease(db, worker.id, job_id)
    _assert_worker_job_access(worker, job)
    job.lease_expires_at = datetime.utcnow() + timedelta(seconds=payload.lease_seconds)
    db.commit()
    db.refresh(job)
    return job


@router.get("/channels/{channel_id}/credentials")
def channel_credentials(
    channel_id: int,
    db: Session = Depends(get_db),
    worker: WorkerToken = Depends(get_current_worker),
):
    allowed_ids = _worker_allowed_ids(worker)
    if allowed_ids is not None and channel_id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Channel access denied",
        )
    channel = (
        db.query(YouTubeChannel)
        .filter(YouTubeChannel.id == channel_id, YouTubeChannel.is_active.is_(True))
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    try:
        access_token = get_access_token(db, channel)
    except YouTubeCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from None
    return {"access_token": access_token}
