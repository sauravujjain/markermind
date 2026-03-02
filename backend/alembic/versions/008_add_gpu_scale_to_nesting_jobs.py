"""Add gpu_scale to nesting_jobs

Revision ID: 008
Revises: 007
Create Date: 2026-02-23

Changes:
- Add gpu_scale float column to nesting_jobs table
  (Rasterization resolution in px/mm. Default 0.15, use 0.3 for demo quality)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '008'
down_revision: Union[str, None] = '007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'nesting_jobs',
        sa.Column('gpu_scale', sa.Float(), server_default='0.15', nullable=True)
    )


def downgrade() -> None:
    op.drop_column('nesting_jobs', 'gpu_scale')
