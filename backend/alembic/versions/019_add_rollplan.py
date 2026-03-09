"""Add roll_plans and fabric_rolls tables

Revision ID: 019
Revises: 018
Create Date: 2026-03-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = '019'
down_revision: Union[str, None] = '018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # roll_plans table (enums auto-created by sa.Enum with create_type=True)
    op.create_table(
        'roll_plans',
        sa.Column('id', UUID(as_uuid=False), primary_key=True),
        sa.Column('cutplan_id', UUID(as_uuid=False), sa.ForeignKey('cutplans.id'), nullable=False),
        sa.Column('name', sa.String(200), nullable=True),
        sa.Column('color_code', sa.String(50), nullable=True),

        # Status
        sa.Column('status', sa.Enum('pending', 'running', 'completed', 'failed', 'cancelled',
                                     name='rollplanstatus'), server_default='pending'),
        sa.Column('mode', sa.Enum('monte_carlo', 'ga', 'both',
                                   name='rollplanmode'), server_default='both'),

        # Config
        sa.Column('num_simulations', sa.Integer(), server_default='100'),
        sa.Column('min_reuse_length_yards', sa.Float(), server_default='0.5'),
        sa.Column('input_type', sa.Enum('pseudo', 'real', 'mixed',
                                         name='rollinputtype'), server_default='pseudo'),
        sa.Column('pseudo_roll_avg_yards', sa.Float(), server_default='100.0'),
        sa.Column('pseudo_roll_delta_yards', sa.Float(), server_default='20.0'),

        # Progress
        sa.Column('progress', sa.Integer(), server_default='0'),
        sa.Column('progress_message', sa.String(500), server_default=''),
        sa.Column('error_message', sa.Text(), nullable=True),

        # MC results — waste breakdown per category (avg across runs)
        sa.Column('total_fabric_required', sa.Float(), nullable=True),
        # Type 1: unusable scraps (< piece consumption)
        sa.Column('mc_unusable_avg', sa.Float(), nullable=True),
        sa.Column('mc_unusable_std', sa.Float(), nullable=True),
        sa.Column('mc_unusable_p95', sa.Float(), nullable=True),
        # Type 2: end-bit waste (optimization target)
        sa.Column('mc_endbit_avg', sa.Float(), nullable=True),
        sa.Column('mc_endbit_std', sa.Float(), nullable=True),
        sa.Column('mc_endbit_p95', sa.Float(), nullable=True),
        # Type 3: returnable to warehouse (>= longest marker)
        sa.Column('mc_returnable_avg', sa.Float(), nullable=True),
        sa.Column('mc_returnable_std', sa.Float(), nullable=True),
        sa.Column('mc_returnable_p95', sa.Float(), nullable=True),
        # Real waste (Type 1 + Type 2) combined
        sa.Column('mc_real_waste_avg', sa.Float(), nullable=True),
        sa.Column('mc_real_waste_std', sa.Float(), nullable=True),
        sa.Column('mc_real_waste_p95', sa.Float(), nullable=True),
        sa.Column('mc_simulation_runs', sa.JSON(), nullable=True),
        sa.Column('mc_best_run_dockets', sa.JSON(), nullable=True),

        # GA results — waste breakdown
        sa.Column('ga_unusable_yards', sa.Float(), nullable=True),
        sa.Column('ga_endbit_yards', sa.Float(), nullable=True),
        sa.Column('ga_returnable_yards', sa.Float(), nullable=True),
        sa.Column('ga_real_waste_yards', sa.Float(), nullable=True),
        sa.Column('ga_generations_run', sa.Integer(), nullable=True),
        sa.Column('ga_dockets', sa.JSON(), nullable=True),

        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_roll_plans_cutplan_id', 'roll_plans', ['cutplan_id'])

    # fabric_rolls table
    op.create_table(
        'fabric_rolls',
        sa.Column('id', UUID(as_uuid=False), primary_key=True),
        sa.Column('roll_plan_id', UUID(as_uuid=False), sa.ForeignKey('roll_plans.id'), nullable=False),
        sa.Column('roll_number', sa.String(100), nullable=False),
        sa.Column('length_yards', sa.Float(), nullable=False),
        sa.Column('is_pseudo', sa.Boolean(), server_default='false'),
        sa.Column('width_inches', sa.Float(), nullable=True),
        sa.Column('shrinkage_x_pct', sa.Float(), nullable=True),
        sa.Column('shrinkage_y_pct', sa.Float(), nullable=True),
        sa.Column('shade_group', sa.String(50), nullable=True),

        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_fabric_rolls_roll_plan_id', 'fabric_rolls', ['roll_plan_id'])


def downgrade() -> None:
    op.drop_index('ix_fabric_rolls_roll_plan_id', table_name='fabric_rolls')
    op.drop_table('fabric_rolls')
    op.drop_index('ix_roll_plans_cutplan_id', table_name='roll_plans')
    op.drop_table('roll_plans')
    op.execute("DROP TYPE IF EXISTS rollinputtype")
    op.execute("DROP TYPE IF EXISTS rollplanmode")
    op.execute("DROP TYPE IF EXISTS rollplanstatus")
