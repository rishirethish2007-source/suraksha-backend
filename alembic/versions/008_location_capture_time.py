"""Keep GPS capture time separate from SOS heartbeat time.

Revision ID: 008
Revises: 007
"""
from alembic import op
import sqlalchemy as sa

revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def upgrade():
    # Existing coordinates have unknown capture time; do not invent freshness.
    op.add_column('sos_events', sa.Column('location_captured_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('sos_events', 'location_captured_at')
