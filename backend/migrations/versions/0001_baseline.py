"""Baseline: the workspace schema as created by Base.metadata.create_all at this point.

Existing databases are stamped with this revision on first start; later revisions carry real changes
(renames, type changes, data moves) that the additive column sync in db.py cannot do.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-07
"""

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
