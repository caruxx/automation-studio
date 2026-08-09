"""Logged-in user endpoints for creating and inspecting jobs."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import accessible_channel_id_set, get_current_channel, get_current_user
from ..models.db_models import Job, User, YouTubeChannel


router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    job_type: str = Field(min_length=1, max_length=32)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    deadline_at: Optional[datetime] = None
    max_attempts: int = Field(default=3, ge=1, le=100)


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    created_by_user_id: Optional[int]
    job_type: str
    payload: dict[str, Any]
    state: str
    priority: int
    deadline_at: Optional[datetime]
    leased_by_worker_token_id: Optional[int]
    lease_expires_at: Optional[datetime]
    attempts: int
    max_attempts: int
    result: Optional[dict[str, Any]]
    error: Optional[str]
    quota_units: int
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class JobListResponse(BaseModel):
    items: list[JobResponse]
    total: int
    page: int
    page_size: int


def _accessible_job_query(db: Session, user: User):
    query = db.query(Job)
    allowed_ids = accessible_channel_id_set(user)
    if allowed_ids is not None:
        query = query.filter(Job.channel_id.in_(allowed_ids))
    return query


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: CreateJobRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    channel: YouTubeChannel = Depends(get_current_channel),
):
    job = Job(
        channel_id=channel.id,
        created_by_user_id=current_user.id,
        **payload.model_dump(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.get("", response_model=JobListResponse)
def list_jobs(
    state_filter: Optional[str] = Query(default=None, alias="state"),
    job_type: Optional[str] = None,
    channel_id: Optional[int] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = _accessible_job_query(db, current_user)
    if state_filter is not None:
        query = query.filter(Job.state == state_filter)
    if job_type is not None:
        query = query.filter(Job.job_type == job_type)
    if channel_id is not None:
        query = query.filter(Job.channel_id == channel_id)
    total = query.count()
    items = (
        query.order_by(Job.created_at.desc(), Job.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _get_accessible_job_or_404(db: Session, user: User, job_id: int) -> Job:
    job = _accessible_job_query(db, user).filter(Job.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _get_accessible_job_or_404(db, current_user, job_id)


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = _get_accessible_job_or_404(db, current_user, job_id)
    if job.state != "queued":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only queued jobs can be canceled",
        )
    job.state = "canceled"
    job.finished_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job
