"""create weather_forecast table

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-19

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "weather_forecast",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("nx", sa.Integer(), nullable=True),
        sa.Column("ny", sa.Integer(), nullable=True),
        sa.Column("base_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fcst_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wind_speed_ms", sa.Float(), nullable=True),
        sa.Column("wave_height_m", sa.Float(), nullable=True),
        sa.Column("air_temp_c", sa.Float(), nullable=True),
        sa.Column("precip_type_code", sa.Float(), nullable=True),
        sa.Column("sky_code", sa.Float(), nullable=True),
        sa.Column("precip_prob_pct", sa.Float(), nullable=True),
        sa.Column("source_system", sa.String(length=50), nullable=False),
        sa.Column("source_table", sa.String(length=50), nullable=False),
        sa.Column("collected_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_flag", sa.String(length=20), nullable=False, server_default="OK"),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "idx_weather_forecast_natural_key",
        "weather_forecast",
        ["nx", "ny", "fcst_at_utc"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("weather_forecast")
