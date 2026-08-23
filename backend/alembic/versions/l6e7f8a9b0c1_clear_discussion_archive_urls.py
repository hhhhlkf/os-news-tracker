"""clear unused discussion archive URLs

Revision ID: l6e7f8a9b0c1
Revises: k5d6e7f8a9b0
Create Date: 2026-08-16 15:15:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "l6e7f8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "k5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A discussion source is selected by its mail-header rule, never by this
    # inherited common-source URL field.  Remove values collected by the
    # retired "public archive URL" input.
    op.execute("UPDATE sources SET url = '' WHERE stream = 'discussion'")


def downgrade() -> None:
    # Removed archive URLs cannot be reconstructed.
    pass
