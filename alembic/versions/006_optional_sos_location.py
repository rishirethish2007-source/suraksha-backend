"""Allow emergency alerts without a location fix."""
from alembic import op
import sqlalchemy as sa
revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None

def upgrade():
    op.alter_column('sos_events', 'latitude', existing_type=sa.Float(), nullable=True)
    op.alter_column('sos_events', 'longitude', existing_type=sa.Float(), nullable=True)

def downgrade():
    # Refuse rollback while unknown-location emergencies exist; never invent coordinates.
    op.alter_column('sos_events', 'latitude', existing_type=sa.Float(), nullable=False)
    op.alter_column('sos_events', 'longitude', existing_type=sa.Float(), nullable=False)
