"""Add marker_layouts table and cutplan status values

Revision ID: 004
Revises: 003
Create Date: 2026-02-11

Changes:
- Add marker_layouts table for storing final CPU-nested marker layouts
- Add 'refining' and 'refined' values to cutplan_status enum
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = '004'
down_revision: Union[str, None] = '003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new enum values to cutplan_status
    op.execute("ALTER TYPE cutplanstatus ADD VALUE IF NOT EXISTS 'refining'")
    op.execute("ALTER TYPE cutplanstatus ADD VALUE IF NOT EXISTS 'refined'")

    # Create marker_layouts table
    op.create_table(
        'marker_layouts',
        sa.Column('id', UUID(as_uuid=False), primary_key=True),
        sa.Column('cutplan_marker_id', UUID(as_uuid=False), sa.ForeignKey('cutplan_markers.id'), nullable=False, unique=True),
        sa.Column('utilization', sa.Float(), nullable=True),
        sa.Column('strip_length_mm', sa.Float(), nullable=True),
        sa.Column('length_yards', sa.Float(), nullable=True),
        sa.Column('computation_time_s', sa.Float(), nullable=True),
        sa.Column('svg_preview', sa.Text(), nullable=True),
        sa.Column('dxf_file_path', sa.String(500), nullable=True),
        sa.Column('piece_buffer_mm', sa.Float(), nullable=True),
        sa.Column('edge_buffer_mm', sa.Float(), nullable=True),
        sa.Column('time_limit_s', sa.Float(), nullable=True),
        sa.Column('rotation_mode', sa.String(20), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Create index on cutplan_marker_id for fast lookup
    op.create_index('ix_marker_layouts_cutplan_marker_id', 'marker_layouts', ['cutplan_marker_id'])


def downgrade() -> None:
    op.drop_index('ix_marker_layouts_cutplan_marker_id', table_name='marker_layouts')
    op.drop_table('marker_layouts')
    # Note: PostgreSQL doesn't support removing enum values easily
    # The 'refining' and 'refined' enum values will remain
