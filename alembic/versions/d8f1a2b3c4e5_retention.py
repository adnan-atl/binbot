"""retention: users.last_active_at, and referrals survive the friend's account being deleted

Revision ID: d8f1a2b3c4e5
Revises: c3e5a7b9d1f2
Create Date: 2026-11-03 18:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'd8f1a2b3c4e5'
down_revision = 'c3e5a7b9d1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('last_active_at', sa.DateTime(timezone=True), nullable=True))
    # Existing accounts start their clock from the later of sign-up and their last reminder.
    op.execute("""
        UPDATE users SET last_active_at = GREATEST(created_at, COALESCE(
            (SELECT MAX(sent_at) FROM reminders r WHERE r.user_id = users.id AND r.status IN ('sent','done','snoozed')),
            created_at))
    """)
    op.create_index('ix_users_last_active_at', 'users', ['last_active_at'])
    op.alter_column('referrals', 'referred_id', existing_type=sa.String(length=32), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM referrals WHERE referred_id IS NULL")
    op.alter_column('referrals', 'referred_id', existing_type=sa.String(length=32), nullable=False)
    op.drop_index('ix_users_last_active_at', table_name='users')
    op.drop_column('users', 'last_active_at')
