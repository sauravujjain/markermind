"""Add waste_threshold_pct to roll_plans

Revision ID: 022
Revises: 021
Create Date: 2026-03-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '022'
down_revision: Union[str, None] = '021b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('roll_plans', sa.Column('waste_threshold_pct', sa.Float(), server_default='1.5', nullable=True))


def downgrade() -> None:
    op.drop_column('roll_plans', 'waste_threshold_pct')
