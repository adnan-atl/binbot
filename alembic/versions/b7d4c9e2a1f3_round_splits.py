"""round splits

Revision ID: b7d4c9e2a1f3
Revises: a1c2e3f4b5d6
Create Date: 2026-11-01 14:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'b7d4c9e2a1f3'
down_revision = 'a1c2e3f4b5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'round_splits',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('round_key', sa.String(64), nullable=False),
        sa.Column('rule', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_round_splits_round_key', 'round_splits', ['round_key'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_round_splits_round_key', table_name='round_splits')
    op.drop_table('round_splits')
