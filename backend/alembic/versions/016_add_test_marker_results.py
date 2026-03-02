"""Add test_marker_results table

Revision ID: 016
Revises: 015
Create Date: 2026-02-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '016'
down_revision: Union[str, None] = '015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'test_marker_results',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('pattern_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('patterns.id'), nullable=False),
        sa.Column('order_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('orders.id'), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=False), sa.ForeignKey('users.id'), nullable=False),

        # Ratio
        sa.Column('ratio_str', sa.String(100), nullable=False),
        sa.Column('size_bundles', sa.JSON, nullable=False),
        sa.Column('bundle_count', sa.Integer, nullable=False),
        sa.Column('material', sa.String(50), nullable=True),

        # Results
        sa.Column('efficiency', sa.Float, nullable=False),
        sa.Column('length_mm', sa.Float, nullable=False),
        sa.Column('length_yards', sa.Float, nullable=False),
        sa.Column('fabric_width_mm', sa.Float, nullable=False),
        sa.Column('piece_count', sa.Integer, nullable=False),
        sa.Column('computation_time_ms', sa.Float, nullable=False),
        sa.Column('svg_preview', sa.Text, nullable=True),

        # Nesting params snapshot
        sa.Column('time_limit_s', sa.Float, nullable=False),
        sa.Column('quadtree_depth', sa.Integer, nullable=False),
        sa.Column('early_termination', sa.Boolean, nullable=False),
        sa.Column('piece_buffer_mm', sa.Float, nullable=False),
        sa.Column('edge_buffer_mm', sa.Float, nullable=False),
        sa.Column('orientation', sa.String(20), nullable=False),
        sa.Column('exploration_time_s', sa.Integer, nullable=True),
        sa.Column('compression_time_s', sa.Integer, nullable=True),

        # User annotation
        sa.Column('notes', sa.Text, nullable=True),

        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_index('ix_test_marker_results_pattern_id', 'test_marker_results', ['pattern_id'])
    op.create_index('ix_test_marker_results_pattern_order', 'test_marker_results', ['pattern_id', 'order_id'])


def downgrade() -> None:
    op.drop_index('ix_test_marker_results_pattern_order')
    op.drop_index('ix_test_marker_results_pattern_id')
    op.drop_table('test_marker_results')
