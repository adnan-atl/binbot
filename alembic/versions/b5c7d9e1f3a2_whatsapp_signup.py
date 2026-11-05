"""users.wa_step / wa_data: sign-up inside WhatsApp

Revision ID: b5c7d9e1f3a2
Revises: a4b6c8d0e2f1
Create Date: 2026-11-05 12:30:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'b5c7d9e1f3a2'
down_revision = 'a4b6c8d0e2f1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('wa_step', sa.String(length=20), nullable=True))
    op.add_column('users', sa.Column('wa_data', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'wa_data')
    op.drop_column('users', 'wa_step')
