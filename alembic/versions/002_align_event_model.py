"""Align historical PostGIS schema with the application's coordinate model.

Revision ID: 002
Revises: 001
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001_create_sos_table"
branch_labels = None
depends_on = None

def upgrade():
    # Also supports the legacy create_all schema after an explicit baseline stamp.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()
                 AND table_name='sos_events' AND column_name='location') THEN
        ALTER TABLE sos_events ADD COLUMN latitude DOUBLE PRECISION;
        ALTER TABLE sos_events ADD COLUMN longitude DOUBLE PRECISION;
        UPDATE sos_events SET latitude=ST_Y(location::geometry), longitude=ST_X(location::geometry);
        ALTER TABLE sos_events ALTER COLUMN latitude SET NOT NULL;
        ALTER TABLE sos_events ALTER COLUMN longitude SET NOT NULL;
        ALTER TABLE sos_events DROP COLUMN location;
      END IF;
    END $$""")
    for table in ("sos_events", "media_attachments"):
        op.alter_column(table, "id", type_=sa.String(), postgresql_using="id::text")
    for column in ("relay_chain", "media_attachment_ids", "acknowledged_by"):
        op.alter_column("sos_events", column, type_=sa.JSON(), postgresql_using=f"{column}::json")
    op.add_column("sos_events", sa.Column("responder_ids", sa.JSON(), nullable=True))

def downgrade():
    # Deliberately refuse a lossy downgrade of responder identity history.
    raise RuntimeError("Restore a database backup to downgrade revision 002")
