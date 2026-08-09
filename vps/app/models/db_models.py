"""Database models used by the VPS control plane."""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from ..db import Base
from ..services.token_crypto import decrypt_json, encrypt_json

# ワーカートークンのうち、DB に平文で残す先頭文字数。
WORKER_TOKEN_PREFIX_LEN = 8


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


class YouTubeChannel(Base):
    __tablename__ = "youtube_channels"

    id = Column(Integer, primary_key=True)
    channel_key = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(128), nullable=False)
    handle = Column(String(128), nullable=True)
    youtube_channel_id = Column(String(64), index=True, nullable=True)
    prefix = Column(String(32), nullable=True)
    folder_rel = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False, index=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    oauth_client_id = Column(String(128), nullable=True)
    oauth_client_secret_encrypted = Column(Text, nullable=True)
    oauth_refresh_token_encrypted = Column(Text, nullable=True)
    credentials_updated_at = Column(DateTime, nullable=True)

    @property
    def oauth_client_secret(self):
        return decrypt_json(
            self.oauth_client_secret_encrypted,
            field="youtube_channel.oauth_client_secret",
        )

    @oauth_client_secret.setter
    def oauth_client_secret(self, value) -> None:
        self.oauth_client_secret_encrypted = encrypt_json(
            value,
            field="youtube_channel.oauth_client_secret",
        )

    @property
    def oauth_refresh_token(self):
        return decrypt_json(
            self.oauth_refresh_token_encrypted,
            field="youtube_channel.oauth_refresh_token",
        )

    @oauth_refresh_token.setter
    def oauth_refresh_token(self, value) -> None:
        self.oauth_refresh_token_encrypted = encrypt_json(
            value,
            field="youtube_channel.oauth_refresh_token",
        )


class OAuthState(Base):
    __tablename__ = "oauth_states"

    id = Column(Integer, primary_key=True)
    state = Column(String(64), unique=True, index=True, nullable=False)
    channel_id = Column(
        Integer,
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    redirect_uri = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)

    channel = relationship("YouTubeChannel")
    user = relationship("User")


class WorkerToken(Base):
    __tablename__ = "worker_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label = Column(String(128), nullable=False)
    token_hash = Column(String(255), nullable=False)
    # 照合候補を絞るためだけの平文先頭。長くすると平文の露出が増えるので短く保つ。
    # 8 文字(約 48bit)あれば候補は実質1件に絞れ、残り約 27 文字の秘密は保たれる。
    token_prefix = Column(String(WORKER_TOKEN_PREFIX_LEN), nullable=False, index=True)
    last_seen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    disabled_at = Column(DateTime, nullable=True)

    user = relationship("User")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    channel_id = Column(
        Integer,
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_type = Column(String(32), nullable=False, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    state = Column(String(16), nullable=False, default="queued", index=True)
    priority = Column(Integer, nullable=False, default=0)
    deadline_at = Column(DateTime, nullable=True, index=True)
    leased_by_worker_token_id = Column(
        Integer,
        ForeignKey("worker_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    quota_units = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    channel = relationship("YouTubeChannel")
    created_by = relationship("User")
    leased_by_worker_token = relationship("WorkerToken")
