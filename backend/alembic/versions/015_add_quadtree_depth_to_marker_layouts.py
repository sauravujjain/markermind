"""Add quadtree_depth column to marker_layouts

Revision ID: 015
Revises: 014
Create Date: 2026-02-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '015'
down_revision: Union[str, None] = '014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('marker_layouts', sa.Column('quadtree_depth', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('marker_layouts', 'quadtree_depth')
