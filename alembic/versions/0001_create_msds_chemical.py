"""create msds_chemical

Revision ID: 0001
Revises:
Create Date: 2026-07-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "msds_chemical",
        sa.Column("chem_id", sa.String(length=20), primary_key=True),
        sa.Column("cas_no", sa.String(length=50), nullable=True),
        sa.Column("un_no", sa.String(length=20), nullable=True),
        sa.Column("name_ko", sa.String(length=500), nullable=True),
        sa.Column("name_en", sa.String(length=500), nullable=True),
        sa.Column("source_system", sa.String(length=50), nullable=False, server_default="KOSHA_MSDS_API"),
        sa.Column("source_table", sa.String(length=50), nullable=False, server_default="getChemDetail"),
        sa.Column("collected_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_flag", sa.String(length=20), nullable=False, server_default="OK"),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("msds_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )

    op.create_index(
        "idx_msds_chemical_cas_no",
        "msds_chemical",
        ["cas_no"],
        unique=True,
        postgresql_where=sa.text("cas_no IS NOT NULL AND cas_no <> ''"),
    )
    op.create_index(
        "idx_msds_chemical_payload_gin",
        "msds_chemical",
        ["msds_payload"],
        postgresql_using="gin",
    )
    op.create_index(
        "idx_msds_chemical_collected_at",
        "msds_chemical",
        ["collected_at_utc"],
    )


def downgrade() -> None:
    op.drop_index("idx_msds_chemical_collected_at", table_name="msds_chemical")
    op.drop_index("idx_msds_chemical_payload_gin", table_name="msds_chemical")
    op.drop_index("idx_msds_chemical_cas_no", table_name="msds_chemical")
    op.drop_table("msds_chemical")
