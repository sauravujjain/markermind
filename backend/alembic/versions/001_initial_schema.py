"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-02-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create enum types
    op.execute("CREATE TYPE userrole AS ENUM ('admin', 'manager', 'operator')")
    op.execute("CREATE TYPE filetype AS ENUM ('aama', 'gerber', 'lectra')")
    op.execute("CREATE TYPE orderstatus AS ENUM ('draft', 'pending_pattern', 'pending_nesting', 'nesting_in_progress', 'pending_cutplan', 'cutplan_ready', 'approved', 'completed')")
    op.execute("CREATE TYPE rotationmode AS ENUM ('free', 'nap_safe', 'garment_linked')")
    op.execute("CREATE TYPE jobstatus AS ENUM ('pending', 'running', 'completed', 'failed', 'cancelled')")
    op.execute("CREATE TYPE markersourcetype AS ENUM ('gpu_nesting', 'spyrrow', 'manual', 'imported')")
    op.execute("CREATE TYPE cutplanstatus AS ENUM ('draft', 'optimizing', 'ready', 'approved', 'in_production', 'completed')")
    op.execute("CREATE TYPE solvertype AS ENUM ('single_color', 'multicolor_joint', 'two_stage')")

    # Customers table
    op.create_table(
        'customers',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('code', sa.String(50), unique=True, nullable=False),
        sa.Column('settings', postgresql.JSON, default={}),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Users table
    op.create_table(
        'users',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('email', sa.String(255), unique=True, nullable=False, index=True),
        sa.Column('password_hash', sa.String(255), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('role', postgresql.ENUM('admin', 'manager', 'operator', name='userrole', create_type=False), default='operator'),
        sa.Column('is_active', sa.String(1), default='Y'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Sessions table
    op.create_table(
        'sessions',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('token_hash', sa.String(255), nullable=False, index=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Fabrics table
    op.create_table(
        'fabrics',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('code', sa.String(50), nullable=False),
        sa.Column('width_inches', sa.Float, nullable=False),
        sa.Column('cost_per_yard', sa.Float, default=0.0),
        sa.Column('description', sa.String(500)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Styles table
    op.create_table(
        'styles',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('style_number', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255)),
        sa.Column('description', sa.String(500)),
        sa.Column('size_range', postgresql.ARRAY(sa.String), default=[]),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Patterns table
    op.create_table(
        'patterns',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('file_type', postgresql.ENUM('aama', 'gerber', 'lectra', name='filetype', create_type=False), default='aama'),
        sa.Column('dxf_file_path', sa.String(500)),
        sa.Column('rul_file_path', sa.String(500)),
        sa.Column('is_parsed', sa.Boolean, default=False),
        sa.Column('available_sizes', postgresql.ARRAY(sa.String), default=[]),
        sa.Column('available_materials', postgresql.ARRAY(sa.String), default=[]),
        sa.Column('parse_metadata', postgresql.JSON, default={}),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Pattern fabric mappings table
    op.create_table(
        'pattern_fabric_mappings',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('pattern_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('patterns.id'), nullable=False),
        sa.Column('material_name', sa.String(100), nullable=False),
        sa.Column('fabric_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('fabrics.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Orders table
    op.create_table(
        'orders',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('order_number', sa.String(100), nullable=False, index=True),
        sa.Column('style_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('styles.id')),
        sa.Column('pattern_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('patterns.id')),
        sa.Column('status', postgresql.ENUM('draft', 'pending_pattern', 'pending_nesting', 'nesting_in_progress', 'pending_cutplan', 'cutplan_ready', 'approved', 'completed', name='orderstatus', create_type=False), default='draft'),
        sa.Column('piece_buffer_mm', sa.Float, default=2.0),
        sa.Column('edge_buffer_mm', sa.Float, default=5.0),
        sa.Column('rotation_mode', postgresql.ENUM('free', 'nap_safe', 'garment_linked', name='rotationmode', create_type=False), default='free'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Order colors table
    op.create_table(
        'order_colors',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('order_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('orders.id'), nullable=False),
        sa.Column('color_code', sa.String(50), nullable=False),
        sa.Column('color_name', sa.String(100)),
        sa.Column('fabric_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('fabrics.id')),
        sa.Column('material_name', sa.String(100)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Size quantities table
    op.create_table(
        'size_quantities',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('order_color_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('order_colors.id'), nullable=False),
        sa.Column('size_code', sa.String(20), nullable=False),
        sa.Column('quantity', sa.Integer, default=0),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Nesting jobs table
    op.create_table(
        'nesting_jobs',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('order_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('orders.id'), nullable=False),
        sa.Column('pattern_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('patterns.id'), nullable=False),
        sa.Column('status', postgresql.ENUM('pending', 'running', 'completed', 'failed', 'cancelled', name='jobstatus', create_type=False), default='pending'),
        sa.Column('progress', sa.Integer, default=0),
        sa.Column('progress_message', sa.String(255)),
        sa.Column('error_message', sa.Text),
        sa.Column('celery_task_id', sa.String(100)),
        sa.Column('fabric_width_inches', sa.Float),
        sa.Column('max_bundle_count', sa.Integer, default=6),
        sa.Column('top_n_results', sa.Integer, default=10),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Nesting job results table
    op.create_table(
        'nesting_job_results',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('nesting_job_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('nesting_jobs.id'), nullable=False),
        sa.Column('bundle_count', sa.Integer, nullable=False),
        sa.Column('rank', sa.Integer, nullable=False),
        sa.Column('ratio_str', sa.String(50), nullable=False),
        sa.Column('efficiency', sa.Float, nullable=False),
        sa.Column('length_yards', sa.Float, nullable=False),
        sa.Column('length_mm', sa.Float),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Marker bank table
    op.create_table(
        'marker_bank',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('pattern_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('patterns.id'), nullable=False),
        sa.Column('fabric_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('fabrics.id'), nullable=False),
        sa.Column('ratio_str', sa.String(50), nullable=False),
        sa.Column('efficiency', sa.Float, nullable=False),
        sa.Column('length_yards', sa.Float, nullable=False),
        sa.Column('length_mm', sa.Float),
        sa.Column('source_type', postgresql.ENUM('gpu_nesting', 'spyrrow', 'manual', 'imported', name='markersourcetype', create_type=False), default='gpu_nesting'),
        sa.Column('extra_data', postgresql.JSON, default={}),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Cutplans table
    op.create_table(
        'cutplans',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('order_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('orders.id'), nullable=False),
        sa.Column('name', sa.String(100)),
        sa.Column('solver_type', postgresql.ENUM('single_color', 'multicolor_joint', 'two_stage', name='solvertype', create_type=False), default='single_color'),
        sa.Column('status', postgresql.ENUM('draft', 'optimizing', 'ready', 'approved', 'in_production', 'completed', name='cutplanstatus', create_type=False), default='draft'),
        sa.Column('unique_markers', sa.Integer),
        sa.Column('total_cuts', sa.Integer),
        sa.Column('bundle_cuts', sa.Integer),
        sa.Column('total_plies', sa.Integer),
        sa.Column('total_yards', sa.Float),
        sa.Column('efficiency', sa.Float),
        sa.Column('total_cost', sa.Float),
        sa.Column('fabric_cost', sa.Float),
        sa.Column('spreading_cost', sa.Float),
        sa.Column('cutting_cost', sa.Float),
        sa.Column('prep_cost', sa.Float),
        sa.Column('solver_config', postgresql.JSON, default={}),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Cutplan markers table
    op.create_table(
        'cutplan_markers',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('cutplan_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('cutplans.id'), nullable=False),
        sa.Column('marker_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('marker_bank.id')),
        sa.Column('ratio_str', sa.String(50), nullable=False),
        sa.Column('efficiency', sa.Float),
        sa.Column('length_yards', sa.Float),
        sa.Column('plies_by_color', postgresql.JSON, default={}),
        sa.Column('total_plies', sa.Integer),
        sa.Column('cuts', sa.Integer),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Cost configs table
    op.create_table(
        'cost_configs',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('name', sa.String(100), default='Default'),
        sa.Column('fabric_cost_per_yard', sa.Float, default=5.0),
        sa.Column('spreading_cost_per_yard', sa.Float, default=0.10),
        sa.Column('cutting_cost_per_inch', sa.Float, default=0.01),
        sa.Column('prep_cost_per_marker', sa.Float, default=2.0),
        sa.Column('max_ply_height', sa.Integer, default=100),
        sa.Column('min_plies_by_bundle', sa.String(255), default='6:50,5:40,4:30,3:10,2:1,1:1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('cost_configs')
    op.drop_table('cutplan_markers')
    op.drop_table('cutplans')
    op.drop_table('marker_bank')
    op.drop_table('nesting_job_results')
    op.drop_table('nesting_jobs')
    op.drop_table('size_quantities')
    op.drop_table('order_colors')
    op.drop_table('orders')
    op.drop_table('pattern_fabric_mappings')
    op.drop_table('patterns')
    op.drop_table('styles')
    op.drop_table('fabrics')
    op.drop_table('sessions')
    op.drop_table('users')
    op.drop_table('customers')

    op.execute('DROP TYPE solvertype')
    op.execute('DROP TYPE cutplanstatus')
    op.execute('DROP TYPE markersourcetype')
    op.execute('DROP TYPE jobstatus')
    op.execute('DROP TYPE rotationmode')
    op.execute('DROP TYPE orderstatus')
    op.execute('DROP TYPE filetype')
    op.execute('DROP TYPE userrole')
