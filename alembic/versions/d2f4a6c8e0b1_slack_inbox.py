"""Slack inbox: inbox_threads, inbox_replies, users.last_inbound_at

Revision ID: d2f4a6c8e0b1
Revises: c8e0a2b4d6f8
Create Date: 2026-11-05 15:30:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'd2f4a6c8e0b1'
down_revision = 'c8e0a2b4d6f8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('last_inbound_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'inbox_threads',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('phone_e164', sa.String(length=20), nullable=False),
        sa.Column('channel', sa.String(length=40), nullable=False),
        sa.Column('thread_ts', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_inbox_threads_phone_e164', 'inbox_threads', ['phone_e164'])
    op.create_index('ix_inbox_threads_thread_ts', 'inbox_threads', ['thread_ts'], unique=True)
    op.create_table(
        'inbox_replies',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('thread_id', sa.String(length=32), sa.ForeignKey('inbox_threads.id'), nullable=False),
        sa.Column('slack_ts', sa.String(length=40), nullable=False, unique=True),
        sa.Column('approved_by', sa.String(length=40), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_inbox_replies_thread_id', 'inbox_replies', ['thread_id'])


def downgrade() -> None:
    op.drop_index('ix_inbox_replies_thread_id', 'inbox_replies')
    op.drop_table('inbox_replies')
    op.drop_index('ix_inbox_threads_thread_ts', 'inbox_threads')
    op.drop_index('ix_inbox_threads_phone_e164', 'inbox_threads')
    op.drop_table('inbox_threads')
    op.drop_column('users', 'last_inbound_at')
