"""Add use_cloud column to test_marker_results

Revision ID: 017
Revises: 016
Create Date: 2026-02-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '017'
down_revision: Union[str, None] = '016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'test_marker_results',
        sa.Column('use_cloud', sa.Boolean, nullable=False, server_default='false'),
    )


def downgrade() -> None:
    op.drop_column('test_marker_results', 'use_cloud')
