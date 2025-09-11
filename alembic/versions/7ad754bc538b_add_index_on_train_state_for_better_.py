"""Add index on train_state for better query performance

Revision ID: 7ad754bc538b
Revises: bf7d202b044b
Create Date: 2025-09-11 09:58:41.856941

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7ad754bc538b"
down_revision: Union[str, Sequence[str], None] = "bf7d202b044b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index("ix_trains_train_state", "trains", ["train_state"])
    op.create_index("ix_trains_updated_at", "trains", ["updated_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_trains_updated_at", "trains")
    op.drop_index("ix_trains_train_state", "trains")
