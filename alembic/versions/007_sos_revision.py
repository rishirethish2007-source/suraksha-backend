"""Track signed SOS location revisions."""
from alembic import op
import sqlalchemy as sa
revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('sos_events', sa.Column('revision', sa.BigInteger(), nullable=False, server_default='0'))

def downgrade():
    op.drop_column('sos_events', 'revision')
