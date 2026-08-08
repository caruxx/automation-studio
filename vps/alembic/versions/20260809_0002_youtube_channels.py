"""Create first-class YouTube channels.

Revision ID: 20260809_0002
Revises: 20260809_0001
Create Date: 2026-08-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260809_0002"
down_revision: Union[str, None] = "20260809_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "youtube_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("handle", sa.String(length=128), nullable=True),
        sa.Column("youtube_channel_id", sa.String(length=64), nullable=True),
        sa.Column("prefix", sa.String(length=32), nullable=True),
        sa.Column("folder_rel", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("oauth_client_id", sa.String(length=128), nullable=True),
        sa.Column("oauth_client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("oauth_refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("credentials_updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_youtube_channels_channel_key",
        "youtube_channels",
        ["channel_key"],
        unique=True,
    )
    op.create_index(
        "ix_youtube_channels_is_default",
        "youtube_channels",
        ["is_default"],
        unique=False,
    )
    op.create_index(
        "ix_youtube_channels_youtube_channel_id",
        "youtube_channels",
        ["youtube_channel_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_youtube_channels_youtube_channel_id", table_name="youtube_channels")
    op.drop_index("ix_youtube_channels_is_default", table_name="youtube_channels")
    op.drop_index("ix_youtube_channels_channel_key", table_name="youtube_channels")
    op.drop_table("youtube_channels")
