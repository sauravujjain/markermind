"""Add sort_order to size_quantities to preserve Excel column order

Revision ID: 013
Revises: 012
Create Date: 2026-02-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '013'
down_revision: Union[str, None] = '012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('size_quantities', sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('size_quantities', 'sort_order')
