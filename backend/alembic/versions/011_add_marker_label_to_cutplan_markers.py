"""Add marker_label to cutplan_markers

Revision ID: 011
Revises: 010
Create Date: 2026-02-24
"""
from alembic import op
import sqlalchemy as sa

revision = '011'
down_revision = '010'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cutplan_markers', sa.Column('marker_label', sa.String(10), nullable=True))


def downgrade():
    op.drop_column('cutplan_markers', 'marker_label')
