"""assets: prepared data the site serves (the home page map's outlines and IP table)

Revision ID: b2d4f6a8c0e1
Revises: a9c1e3f5b7d9
Create Date: 2026-11-16 13:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'b2d4f6a8c0e1'
down_revision = 'a9c1e3f5b7d9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('assets',
                    sa.Column('name', sa.String(length=64), primary_key=True),
                    sa.Column('data', sa.LargeBinary(), nullable=False),
                    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_table('assets')
