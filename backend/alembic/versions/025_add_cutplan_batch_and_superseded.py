"""Add generation_batch_id to cutplans and superseded status

Revision ID: 025
Revises: 024
Create Date: 2026-03-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '025'
down_revision: Union[str, None] = '024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add generation_batch_id column
    op.add_column('cutplans', sa.Column('generation_batch_id', sa.String(36), nullable=True))
    op.create_index('ix_cutplans_generation_batch_id', 'cutplans', ['generation_batch_id'])

    # Add 'superseded' to cutplanstatus enum
    op.execute("ALTER TYPE cutplanstatus ADD VALUE IF NOT EXISTS 'superseded'")


def downgrade() -> None:
    op.drop_index('ix_cutplans_generation_batch_id', table_name='cutplans')
    op.drop_column('cutplans', 'generation_batch_id')
    # Note: PostgreSQL does not support removing enum values
