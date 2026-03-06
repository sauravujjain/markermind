"""Add dxf_data column to test_marker_results

Revision ID: 020
Revises: 019
Create Date: 2026-03-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '020'
down_revision: Union[str, None] = '019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'test_marker_results',
        sa.Column('dxf_data', sa.LargeBinary, nullable=True),
    )


def downgrade() -> None:
    op.drop_column('test_marker_results', 'dxf_data')
