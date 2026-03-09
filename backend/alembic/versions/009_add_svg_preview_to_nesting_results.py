"""Add svg_preview to nesting_job_results

Revision ID: 009
Revises: 008
Create Date: 2026-02-23

Changes:
- Add svg_preview text column to nesting_job_results table
  (SVG string of marker layout for vector preview)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '009'
down_revision: Union[str, None] = '008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'nesting_job_results',
        sa.Column('svg_preview', sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('nesting_job_results', 'svg_preview')
