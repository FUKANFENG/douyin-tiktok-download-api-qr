"""Remember local QR imports so retries do not duplicate identities.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_qr_imports",
        sa.Column("account_id", sa.Text(), primary_key=True),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("local_qr_imports")
