"""Add spreading input parameters to cost_configs

Revision ID: 005
Revises: 004
Create Date: 2026-02-12

Changes:
- Add spreading_labor_cost_per_hour, spreading_speed_m_per_min,
  spreading_prep_buffer_pct, spreading_workers_per_lay, ply_end_cut_time_s
  columns to cost_configs table
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '005'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('cost_configs', sa.Column('spreading_labor_cost_per_hour', sa.Float(), server_default='1.0', nullable=True))
    op.add_column('cost_configs', sa.Column('spreading_speed_m_per_min', sa.Float(), server_default='20.0', nullable=True))
    op.add_column('cost_configs', sa.Column('spreading_prep_buffer_pct', sa.Float(), server_default='20.0', nullable=True))
    op.add_column('cost_configs', sa.Column('spreading_workers_per_lay', sa.Integer(), server_default='2', nullable=True))
    op.add_column('cost_configs', sa.Column('ply_end_cut_time_s', sa.Float(), server_default='20.0', nullable=True))


def downgrade() -> None:
    op.drop_column('cost_configs', 'ply_end_cut_time_s')
    op.drop_column('cost_configs', 'spreading_workers_per_lay')
    op.drop_column('cost_configs', 'spreading_prep_buffer_pct')
    op.drop_column('cost_configs', 'spreading_speed_m_per_min')
    op.drop_column('cost_configs', 'spreading_labor_cost_per_hour')
