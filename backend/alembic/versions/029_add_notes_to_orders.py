"""Add notes column to orders table"""
from alembic import op
import sqlalchemy as sa

revision = '029'
down_revision = '028'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('orders', sa.Column('notes', sa.String(500), nullable=True))

def downgrade():
    op.drop_column('orders', 'notes')
