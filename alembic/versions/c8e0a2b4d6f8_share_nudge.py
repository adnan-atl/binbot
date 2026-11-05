"""users.share_nudged_at: the one "share with neighbours" message

Revision ID: c8e0a2b4d6f8
Revises: b5c7d9e1f3a2
Create Date: 2026-11-05 14:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'c8e0a2b4d6f8'
down_revision = 'b5c7d9e1f3a2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('share_nudged_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'share_nudged_at')
