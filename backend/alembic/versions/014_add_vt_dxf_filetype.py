"""Add vt_dxf to filetype enum

Revision ID: 014
Revises: 013
Create Date: 2026-02-26

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '014'
down_revision: Union[str, None] = '013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE filetype ADD VALUE IF NOT EXISTS 'vt_dxf'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values directly.
    # A full enum rebuild would be needed; skip for safety.
    pass
