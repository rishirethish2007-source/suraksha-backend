"""Restore indexed PostGIS geography and add transactional delivery outbox."""
from alembic import op
import sqlalchemy as sa
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("ALTER TABLE sos_events ADD COLUMN location geography(Point,4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude,latitude),4326)::geography) STORED")
    op.execute("CREATE INDEX ix_sos_events_location ON sos_events USING GIST(location)")
    op.create_table("sos_outbox", sa.Column("id", sa.String(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))

def downgrade():
    op.drop_table("sos_outbox")
    op.drop_index("ix_sos_events_location", table_name="sos_events")
    op.drop_column("sos_events", "location")
