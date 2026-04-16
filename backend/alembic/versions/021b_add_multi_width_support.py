"""Add multi-width support to nesting jobs, results, and marker bank

Revision ID: 021b
Revises: 021
Create Date: 2026-03-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '021b'
down_revision: Union[str, None] = '021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NestingJob: add fabric_widths JSON column for multi-width mode
    op.add_column(
        'nesting_jobs',
        sa.Column('fabric_widths', sa.JSON, nullable=True),
    )

    # NestingJobResult: add fabric_width_inches to track which width each result is for
    op.add_column(
        'nesting_job_results',
        sa.Column('fabric_width_inches', sa.Float, nullable=True),
    )

    # MarkerBank: add fabric_width_inches for width-aware dedup
    op.add_column(
        'marker_bank',
        sa.Column('fabric_width_inches', sa.Float, nullable=True),
    )


def downgrade() -> None:
    op.drop_column('marker_bank', 'fabric_width_inches')
    op.drop_column('nesting_job_results', 'fabric_width_inches')
    op.drop_column('nesting_jobs', 'fabric_widths')
