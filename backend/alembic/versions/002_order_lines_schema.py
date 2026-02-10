"""Order lines schema update

Revision ID: 002
Revises: 001
Create Date: 2026-02-09

Changes:
- Rename order_colors to order_lines
- Add fabric_code column
- Add extra_percent column
- Add style_number to orders table
- Update foreign key references
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add style_number to orders table
    op.add_column('orders', sa.Column('style_number', sa.String(100), nullable=True))

    # Rename order_colors table to order_lines
    op.rename_table('order_colors', 'order_lines')

    # Add new columns to order_lines
    op.add_column('order_lines', sa.Column('fabric_code', sa.String(50), nullable=True))
    op.add_column('order_lines', sa.Column('extra_percent', sa.Float, server_default='0.0'))

    # Update fabric_code from existing material_name or set default
    op.execute("UPDATE order_lines SET fabric_code = COALESCE(material_name, 'DEFAULT') WHERE fabric_code IS NULL")

    # Make fabric_code not nullable after populating
    op.alter_column('order_lines', 'fabric_code', nullable=False)

    # Rename material_name column (keep for now as it may have data)
    # op.drop_column('order_lines', 'material_name')
    # op.drop_column('order_lines', 'color_name')

    # Update size_quantities foreign key
    op.drop_constraint('size_quantities_order_color_id_fkey', 'size_quantities', type_='foreignkey')
    op.alter_column('size_quantities', 'order_color_id', new_column_name='order_line_id')
    op.create_foreign_key(
        'size_quantities_order_line_id_fkey',
        'size_quantities',
        'order_lines',
        ['order_line_id'],
        ['id']
    )


def downgrade() -> None:
    # Revert size_quantities foreign key
    op.drop_constraint('size_quantities_order_line_id_fkey', 'size_quantities', type_='foreignkey')
    op.alter_column('size_quantities', 'order_line_id', new_column_name='order_color_id')
    op.create_foreign_key(
        'size_quantities_order_color_id_fkey',
        'size_quantities',
        'order_lines',
        ['order_color_id'],
        ['id']
    )

    # Drop new columns
    op.drop_column('order_lines', 'extra_percent')
    op.drop_column('order_lines', 'fabric_code')

    # Rename table back
    op.rename_table('order_lines', 'order_colors')

    # Drop style_number from orders
    op.drop_column('orders', 'style_number')
