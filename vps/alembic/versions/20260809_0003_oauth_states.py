"""Create one-time OAuth states.

Revision ID: 20260809_0003
Revises: 20260809_0002
Create Date: 2026-08-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260809_0003"
down_revision: Union[str, None] = "20260809_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["youtube_channels.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_states_state", "oauth_states", ["state"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_oauth_states_state", table_name="oauth_states")
    op.drop_table("oauth_states")
