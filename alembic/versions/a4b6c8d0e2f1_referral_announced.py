"""referrals.announced_at: when the referrer heard about it (news rides on the next reminder)

Revision ID: a4b6c8d0e2f1
Revises: e2a9c4d1f7b3
Create Date: 2026-11-04 19:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'a4b6c8d0e2f1'
down_revision = 'e2a9c4d1f7b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('referrals', sa.Column('announced_at', sa.DateTime(timezone=True), nullable=True))
    # Referrals made before this change: treat as already shared, so nobody gets stale news.
    op.execute("UPDATE referrals SET announced_at = created_at")


def downgrade() -> None:
    op.drop_column('referrals', 'announced_at')
