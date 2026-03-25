"""Add gerber_aama to FileType enum

Revision ID: 028
Revises: 027
Create Date: 2026-03-25

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '028'
down_revision: Union[str, None] = '027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 'gerber_aama' to the PostgreSQL enum type
    op.execute("ALTER TYPE filetype ADD VALUE IF NOT EXISTS 'gerber_aama'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; no-op
    pass
