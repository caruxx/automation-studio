"""Database models used by the VPS control plane."""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from ..db import Base
from ..services.token_crypto import decrypt_json, encrypt_json


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
