"""Add seed_used and seed_screening columns to test_marker_results

Revision ID: 018
Revises: 017
Create Date: 2026-03-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '018'
down_revision: Union[str, None] = '017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'test_marker_results',
        sa.Column('seed_used', sa.Integer, nullable=True),
    )
    op.add_column(
        'test_marker_results',
        sa.Column('seed_screening', sa.Boolean, nullable=False, server_default='false'),
    )


def downgrade() -> None:
    op.drop_column('test_marker_results', 'seed_screening')
    op.drop_column('test_marker_results', 'seed_used')
