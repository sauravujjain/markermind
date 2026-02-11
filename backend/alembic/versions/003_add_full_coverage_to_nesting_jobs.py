"""Add full_coverage to nesting_jobs

Revision ID: 003
Revises: 002
Create Date: 2026-02-10

Changes:
- Add full_coverage boolean column to nesting_jobs table
  (If True, evaluate ALL ratios using brute force instead of GA)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '003'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add full_coverage column to nesting_jobs table
    op.add_column(
        'nesting_jobs',
        sa.Column('full_coverage', sa.Boolean(), server_default='false', nullable=False)
    )


def downgrade() -> None:
    op.drop_column('nesting_jobs', 'full_coverage')
