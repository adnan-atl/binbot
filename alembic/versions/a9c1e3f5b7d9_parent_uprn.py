"""properties.parent_uprn: a flat's building, for councils that keep communal bins against it

Revision ID: a9c1e3f5b7d9
Revises: c9e1a3b5d7f9
Create Date: 2026-11-15 20:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'a9c1e3f5b7d9'
down_revision = 'c9e1a3b5d7f9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('properties', sa.Column('parent_uprn', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('properties', 'parent_uprn')
