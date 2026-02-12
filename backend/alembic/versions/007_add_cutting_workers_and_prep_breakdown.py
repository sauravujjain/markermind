"""Add cutting workers and prep cost breakdown to cost_configs

Revision ID: 007
Revises: 006
Create Date: 2026-02-12

Changes:
- Add cutting_workers_per_cut
- Add prep cost breakdown: per-meter total, 3 paper types with cost + enabled flag
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '007'
down_revision: Union[str, None] = '006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('cost_configs', sa.Column('cutting_workers_per_cut', sa.Integer(), server_default='1', nullable=True))
    op.add_column('cost_configs', sa.Column('prep_cost_per_meter', sa.Float(), server_default='0.25', nullable=True))
    op.add_column('cost_configs', sa.Column('prep_perf_paper_cost_per_m', sa.Float(), server_default='0.1', nullable=True))
    op.add_column('cost_configs', sa.Column('prep_perf_paper_enabled', sa.Boolean(), server_default='true', nullable=True))
    op.add_column('cost_configs', sa.Column('prep_underlayer_cost_per_m', sa.Float(), server_default='0.1', nullable=True))
    op.add_column('cost_configs', sa.Column('prep_underlayer_enabled', sa.Boolean(), server_default='true', nullable=True))
    op.add_column('cost_configs', sa.Column('prep_top_layer_cost_per_m', sa.Float(), server_default='0.05', nullable=True))
    op.add_column('cost_configs', sa.Column('prep_top_layer_enabled', sa.Boolean(), server_default='true', nullable=True))


def downgrade() -> None:
    op.drop_column('cost_configs', 'prep_top_layer_enabled')
    op.drop_column('cost_configs', 'prep_top_layer_cost_per_m')
    op.drop_column('cost_configs', 'prep_underlayer_enabled')
    op.drop_column('cost_configs', 'prep_underlayer_cost_per_m')
    op.drop_column('cost_configs', 'prep_perf_paper_enabled')
    op.drop_column('cost_configs', 'prep_perf_paper_cost_per_m')
    op.drop_column('cost_configs', 'prep_cost_per_meter')
    op.drop_column('cost_configs', 'cutting_workers_per_cut')
