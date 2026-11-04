"""user_bin_settings.colour: the colour a resident picks for a bin they add

Revision ID: e2a9c4d1f7b3
Revises: d8f1a2b3c4e5
Create Date: 2026-11-04 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'e2a9c4d1f7b3'
down_revision = 'd8f1a2b3c4e5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_bin_settings', sa.Column('colour', sa.String(length=24), nullable=True))


def downgrade() -> None:
    op.drop_column('user_bin_settings', 'colour')
