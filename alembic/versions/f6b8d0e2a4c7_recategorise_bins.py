"""Re-run the bin categoriser over stored bin types: "Non-Recyclable Refuse" was filed as
recycling and "Mixed Recycling (… & Glass)" as glass. Fixes the residents' bin settings too.

Revision ID: f6b8d0e2a4c7
Revises: e4a6c8e0b2d3
Create Date: 2026-11-06 00:10:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6b8d0e2a4c7'
down_revision = 'e4a6c8e0b2d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.services.normalize import categorise

    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT b.id, b.council_label, b.category, c.gss_code FROM bin_types b "
        "LEFT JOIN councils c ON c.id = b.council_id")).fetchall()
    for bid, label, old, gss in rows:
        new = categorise(label or "", gss).name
        if new == "other" or new == old:
            continue  # never undo a category we learned from the council's colours
        conn.execute(sa.text("UPDATE bin_types SET category = :c WHERE id = :i"), {"c": new, "i": bid})
        conn.execute(sa.text("UPDATE user_bin_settings SET category = :c "
                             "WHERE bin_type_id = :i AND override_rule IS NULL"), {"c": new, "i": bid})


def downgrade() -> None:
    pass
