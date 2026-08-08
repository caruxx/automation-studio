"""Database models used by the phase 1 control plane."""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship

from ..db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    password_changed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    name = Column(String(255), nullable=True)
    role = Column(String(32), nullable=False, default="user")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    last_failed_login_at = Column(DateTime, nullable=True)
    last_login_ip = Column(String(64), nullable=True)
    last_login_ua = Column(String(512), nullable=True)
    totp_secret = Column(String(64), nullable=True)
    totp_enabled = Column(Boolean, nullable=False, default=False)
    totp_recovery_codes = Column(JSON, nullable=True)
    accessible_channel_ids = Column(JSON, nullable=True)


class UserInvitation(Base):
    __tablename__ = "user_invitations"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), index=True, nullable=False)
    role = Column(String(32), nullable=False, default="user")
    token = Column(String(64), unique=True, index=True, nullable=False)
    invited_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at = Column(DateTime, nullable=False)
    accepted_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    invited_by = relationship("User", foreign_keys=[invited_by_user_id])
