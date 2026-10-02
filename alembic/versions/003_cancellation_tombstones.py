"""Persist cancellations received before relayed alerts."""
from alembic import op
import sqlalchemy as sa
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("sos_cancellations",
        sa.Column("sos_id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))

def downgrade():
    op.drop_table("sos_cancellations")
