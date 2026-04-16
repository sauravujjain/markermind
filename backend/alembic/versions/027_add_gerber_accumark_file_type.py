"""Add gerber_accumark to FileType enum

Revision ID: 027
Revises: 026
Create Date: 2026-03-22

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '027'
down_revision: Union[str, None] = '026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 'gerber_accumark' to the PostgreSQL enum type
    op.execute("ALTER TYPE filetype ADD VALUE IF NOT EXISTS 'gerber_accumark'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; no-op
    pass
