"""Initial messenger schema."""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None


def upgrade():
    op.create_table("users", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("login", sa.String(32), nullable=False, unique=True),
                    sa.Column("password_hash", sa.String(256), nullable=False))
    op.create_table("sessions", sa.Column("token_hash", sa.String(64), primary_key=True),
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("dialogs", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("first_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("second_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.UniqueConstraint("first_id", "second_id"))
    op.create_table("messages", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id"), nullable=False),
                    sa.Column("sender_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("client_id", sa.String(36), nullable=False),
                    sa.Column("text", sa.Text, nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                    sa.UniqueConstraint("sender_id", "client_id"))
    for table, columns in {"sessions": ["user_id", "expires_at"],
                           "dialogs": ["first_id", "second_id"],
                           "messages": ["dialog_id", "expires_at"]}.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in ("messages", "dialogs", "sessions", "users"):
        op.drop_table(table)
