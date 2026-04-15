"""Add roll_adjustment_message, change waste_threshold_pct default to 2.0

Revision ID: 024
Revises: 023
Create Date: 2026-03-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '024'
down_revision: Union[str, None] = '023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('roll_plans', sa.Column('roll_adjustment_message', sa.String(500), nullable=True))
    op.alter_column('roll_plans', 'waste_threshold_pct', server_default='2.0')


def downgrade() -> None:
    op.alter_column('roll_plans', 'waste_threshold_pct', server_default='1.0')
    op.drop_column('roll_plans', 'roll_adjustment_message')
