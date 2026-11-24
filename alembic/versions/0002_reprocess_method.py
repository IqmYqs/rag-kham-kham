"""files.reprocess_method: how a queued reprocess should convert (US-C04 AC4).

NULL = normal conversion; 'default' / 'llm' = requested via POST /files/{id}/reprocess.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE files ADD COLUMN reprocess_method text"
        " CHECK (reprocess_method IN ('default', 'llm'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE files DROP COLUMN reprocess_method")
