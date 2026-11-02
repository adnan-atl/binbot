"""user start code (sign-up ends with the resident saying hi on WhatsApp)

Revision ID: c3e5a7b9d1f2
Revises: b7d4c9e2a1f3
Create Date: 2026-11-02 18:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3e5a7b9d1f2'
down_revision = 'b7d4c9e2a1f3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('start_code', sa.String(length=8), nullable=True))
    op.create_index('ix_users_start_code', 'users', ['start_code'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_users_start_code', table_name='users')
    op.drop_column('users', 'start_code')
