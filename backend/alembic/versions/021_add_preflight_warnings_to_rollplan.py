"""Add preflight_warnings JSON column to roll_plans

Revision ID: 021
Revises: 020
Create Date: 2026-03-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '021'
down_revision: Union[str, None] = '020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('roll_plans', sa.Column('preflight_warnings', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('roll_plans', 'preflight_warnings')
