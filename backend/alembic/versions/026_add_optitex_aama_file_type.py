"""Add optitex_aama to FileType enum

Revision ID: 026
Revises: 025
Create Date: 2026-03-22

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '026'
down_revision: Union[str, None] = '025'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 'optitex_aama' to the PostgreSQL enum type
    op.execute("ALTER TYPE filetype ADD VALUE IF NOT EXISTS 'optitex_aama'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; no-op
    pass
