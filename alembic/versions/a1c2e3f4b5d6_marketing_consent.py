"""marketing consent

Revision ID: a1c2e3f4b5d6
Revises: f248e839ba20
Create Date: 2026-10-31 21:30:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1c2e3f4b5d6'
down_revision = 'f248e839ba20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('marketing_consent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'marketing_consent_at')
