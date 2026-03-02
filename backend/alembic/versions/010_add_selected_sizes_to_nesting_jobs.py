"""Add selected_sizes to nesting_jobs

Revision ID: 010
Revises: 009
Create Date: 2026-02-24

Changes:
- Add selected_sizes JSON column to nesting_jobs table
  (Optional subset of pattern sizes to nest; NULL = all sizes)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '010'
down_revision: Union[str, None] = '009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'nesting_jobs',
        sa.Column('selected_sizes', sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('nesting_jobs', 'selected_sizes')
