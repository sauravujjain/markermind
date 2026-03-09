"""Add dxf_only to filetype enum

Revision ID: 012
Revises: 011
Create Date: 2026-02-24

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '012'
down_revision: Union[str, None] = '011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE filetype ADD VALUE IF NOT EXISTS 'dxf_only'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values directly.
    # A full enum rebuild would be needed; skip for safety.
    pass
