"""users.lookup_seconds / lookup_source: how long the sign-up checker took (sign-ups channel)

Revision ID: a7c9e1b3d5f6
Revises: f6b8d0e2a4c7
Create Date: 2026-11-06 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7c9e1b3d5f6'
down_revision = 'f6b8d0e2a4c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('lookup_seconds', sa.Float(), nullable=True))
    op.add_column('users', sa.Column('lookup_source', sa.String(12), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'lookup_source')
    op.drop_column('users', 'lookup_seconds')
