"""Reminder attempts: failed reminders are retried before the team is alerted.

Revision ID: c9e1a3b5d7f9
Revises: b8d0f2a4c6e8
Create Date: 2026-11-06 14:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'c9e1a3b5d7f9'
down_revision = 'b8d0f2a4c6e8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('reminders', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('reminders', 'attempts')
