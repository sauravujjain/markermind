"""Add pseudo_buffer_pct to roll_plans

Revision ID: 023
Revises: 022
Create Date: 2026-03-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '023'
down_revision: Union[str, None] = '022'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('roll_plans', sa.Column('pseudo_buffer_pct', sa.Float(), server_default='5.0', nullable=True))


def downgrade() -> None:
    op.drop_column('roll_plans', 'pseudo_buffer_pct')
