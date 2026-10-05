"""Individual accounts and persistent revocable sessions."""
from alembic import op
import sqlalchemy as sa
revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('accounts', sa.Column('id',sa.String(),primary_key=True),
        sa.Column('email',sa.String(254),nullable=False,unique=True), sa.Column('name',sa.String(200),nullable=False),
        sa.Column('phone',sa.String(32),nullable=False), sa.Column('password_hash',sa.String(),nullable=False),
        sa.Column('role',sa.String(20),nullable=False), sa.Column('enabled',sa.Boolean(),nullable=False))
    op.create_table('account_sessions',sa.Column('token_hash',sa.String(64),primary_key=True),
        sa.Column('account_id',sa.String(),sa.ForeignKey('accounts.id',ondelete='CASCADE'),nullable=False),
        sa.Column('created_at',sa.DateTime(),server_default=sa.func.now(),nullable=False))
    op.create_index('ix_account_sessions_account_id','account_sessions',['account_id'])

def downgrade():
    op.drop_table('account_sessions')
    op.drop_table('accounts')
