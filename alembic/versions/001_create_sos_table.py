"""
Alembic migration to create SOS and media attachments tables with PostGIS.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import geoalchemy2

# revision identifiers, used by Alembic.
revision: str = '001_create_sos_table'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ensure postgis extension is created
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    
    # Create sos_events table
    op.create_table(
        'sos_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('sos_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('user_name', sa.String(), nullable=False),
        sa.Column('user_phone', sa.String(), nullable=True),
        sa.Column('sos_type', sa.String(), nullable=False),
        sa.Column('message_type', sa.String(), nullable=False),
        sa.Column('location', geoalchemy2.types.Geography(geometry_type='POINT', srid=4326, from_text='ST_GeogFromText', name='geography', spatial_index=True), nullable=False),
        sa.Column('altitude', sa.Float(), nullable=True),
        sa.Column('location_accuracy', sa.Float(), nullable=True),
        sa.Column('location_provider', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('delivery_method', sa.String(), nullable=False),
        sa.Column('hop_count', sa.Integer(), nullable=False),
        sa.Column('max_hops', sa.Integer(), nullable=False),
        sa.Column('relay_chain', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('media_attachment_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('origin_device_id', sa.String(), nullable=True),
        sa.Column('ttl_seconds', sa.Integer(), nullable=False),
        sa.Column('client_timestamp', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancellation_reason', sa.Text(), nullable=True),
        sa.Column('acknowledged_by', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('responders_en_route', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sos_events_sos_id'), 'sos_events', ['sos_id'], unique=True)
    op.create_index(op.f('ix_sos_events_user_id'), 'sos_events', ['user_id'], unique=False)
    op.create_index(op.f('ix_sos_events_status'), 'sos_events', ['status'], unique=False)
    op.create_index(op.f('ix_sos_events_created_at'), 'sos_events', ['created_at'], unique=False)
    op.create_index('idx_sos_status_created_at', 'sos_events', ['status', 'created_at'], unique=False)

    # Create media_attachments table
    op.create_table(
        'media_attachments',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('sos_id', sa.String(), nullable=False),
        sa.Column('file_url', sa.String(), nullable=False),
        sa.Column('file_type', sa.String(), nullable=False),
        sa.Column('file_size_bytes', sa.Integer(), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('uploaded_by', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['sos_id'], ['sos_events.sos_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_media_attachments_sos_id'), 'media_attachments', ['sos_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_media_attachments_sos_id'), table_name='media_attachments')
    op.drop_table('media_attachments')
    op.drop_index('idx_sos_status_created_at', table_name='sos_events')
    op.drop_index(op.f('ix_sos_events_created_at'), table_name='sos_events')
    op.drop_index(op.f('ix_sos_events_status'), table_name='sos_events')
    op.drop_index(op.f('ix_sos_events_user_id'), table_name='sos_events')
    op.drop_index(op.f('ix_sos_events_sos_id'), table_name='sos_events')
    # Use drop_table from geoalchemy to safely drop spatial tables/indexes if needed, but standard drop_table works for pure extensions
    op.drop_table('sos_events')
    op.execute("DROP EXTENSION IF EXISTS postgis;")
