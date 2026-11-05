"""users.hi_prompted_at / signup_nudged_at: nudge website sign-ups that never said hi

Revision ID: e4a6c8e0b2d3
Revises: d2f4a6c8e0b1
Create Date: 2026-11-05 17:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'e4a6c8e0b2d3'
down_revision = 'd2f4a6c8e0b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('hi_prompted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('signup_nudged_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'signup_nudged_at')
    op.drop_column('users', 'hi_prompted_at')
