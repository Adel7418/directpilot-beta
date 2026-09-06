"""Grant local Yandex connection deletion to the runtime app role."""

from __future__ import annotations

from alembic import op

revision = "20260906_0009"
down_revision = "20260906_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT DELETE ON TABLE yandex_provider_connections TO directpilot_app")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON TABLE yandex_provider_connections FROM directpilot_app")
