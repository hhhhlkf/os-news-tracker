"""make Discovery experience admission idempotent

Revision ID: a1f2e3d4c5b6
Revises: z0e1f2a3b4c5
Create Date: 2026-08-21
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a1f2e3d4c5b6"
down_revision: Union[str, Sequence[str], None] = "z0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Retain the oldest admitted provenance if a pre-constraint deployment
    # observed concurrent approvals. NULL sources are not valid read-side
    # provenance and remain unconstrained for safe historical migration.
    op.execute(
        """
        DELETE FROM discovery_experiences
        WHERE source_method_id IS NOT NULL
          AND id NOT IN (
              SELECT MIN(id)
              FROM discovery_experiences
              WHERE source_method_id IS NOT NULL
              GROUP BY experience_kind, source_method_id
          )
        """
    )
    with op.batch_alter_table("discovery_experiences") as batch_op:
        batch_op.create_unique_constraint(
            "uq_discovery_experiences_kind_source_method",
            ["experience_kind", "source_method_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("discovery_experiences") as batch_op:
        batch_op.drop_constraint(
            "uq_discovery_experiences_kind_source_method",
            type_="unique",
        )
