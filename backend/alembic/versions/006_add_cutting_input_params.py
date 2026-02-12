"""Add cutting input parameters to cost_configs

Revision ID: 006
Revises: 005
Create Date: 2026-02-12

Changes:
- Add cutting_speed_cm_per_s and cutting_labor_cost_per_hour columns
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '006'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('cost_configs', sa.Column('cutting_speed_cm_per_s', sa.Float(), server_default='10.0', nullable=True))
    op.add_column('cost_configs', sa.Column('cutting_labor_cost_per_hour', sa.Float(), server_default='1.0', nullable=True))


def downgrade() -> None:
    op.drop_column('cost_configs', 'cutting_labor_cost_per_hour')
    op.drop_column('cost_configs', 'cutting_speed_cm_per_s')
