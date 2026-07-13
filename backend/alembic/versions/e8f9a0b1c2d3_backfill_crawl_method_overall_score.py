"""backfill crawl method overall score

Revision ID: e8f9a0b1c2d3
Revises: e7f8a9b0c1d2
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE crawl_methods
        SET overall_score = CAST(ROUND((quality_score * 0.5) + (COALESCE(density_score, quality_score) * 0.5)) AS INTEGER)
        WHERE overall_score IS NULL
          AND quality_score IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE crawl_methods
        SET quality_grade = CASE
            WHEN overall_score >= 85 THEN 'A'
            WHEN overall_score >= 70 THEN 'B'
            WHEN overall_score >= 50 THEN 'C'
            ELSE 'D'
        END
        WHERE overall_score IS NOT NULL
        """
    )


def downgrade() -> None:
    pass
