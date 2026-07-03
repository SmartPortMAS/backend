"""create ais/portmis/tide/wave/weather collector tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-03

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _common_metadata_columns() -> list[sa.Column]:
    return [
        sa.Column("source_system", sa.String(length=50), nullable=False),
        sa.Column("source_table", sa.String(length=50), nullable=False),
        sa.Column("collected_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_flag", sa.String(length=20), nullable=False, server_default="OK"),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
    ]


def upgrade() -> None:
    op.create_table(
        "ais_vessel_position",
        sa.Column("record_uid", sa.String(length=32), primary_key=True),
        sa.Column("mmsi", sa.Integer(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("sog", sa.Float(), nullable=True),
        sa.Column("cog", sa.Float(), nullable=True),
        sa.Column("nav_status_code", sa.String(length=10), nullable=True),
        sa.Column("nav_status_category", sa.String(length=20), nullable=True),
        sa.Column("received_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ulsan_bound", sa.Boolean(), nullable=True),
        *_common_metadata_columns(),
    )

    op.create_table(
        "ais_vessel_static",
        sa.Column("mmsi", sa.Integer(), primary_key=True),
        sa.Column("imo_no", sa.String(length=20), nullable=True),
        sa.Column("callsgn", sa.String(length=20), nullable=True),
        sa.Column("vessel_name", sa.String(length=200), nullable=True),
        sa.Column("ship_type", sa.String(length=10), nullable=True),
        sa.Column("length", sa.Float(), nullable=True),
        sa.Column("width", sa.Float(), nullable=True),
        sa.Column("draught", sa.Float(), nullable=True),
        sa.Column("Destination", sa.String(length=200), nullable=True),
        sa.Column("ulsan_bound", sa.Boolean(), nullable=True),
        sa.Column("is_liquid_cargo_vessel", sa.Boolean(), nullable=True),
        *_common_metadata_columns(),
    )

    op.create_table(
        "portmis_vessel",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("port_agency_cd", sa.String(length=20), nullable=True),
        sa.Column("port_agency_nm", sa.String(length=100), nullable=True),
        sa.Column("port_agency_label", sa.String(length=100), nullable=True),
        sa.Column("entry_year", sa.Integer(), nullable=True),
        sa.Column("entry_count", sa.Integer(), nullable=True),
        sa.Column("callsgn", sa.String(length=20), nullable=True),
        sa.Column("vessel_name", sa.String(length=200), nullable=True),
        sa.Column("nationality_cd", sa.String(length=10), nullable=True),
        sa.Column("nationality_nm", sa.String(length=100), nullable=True),
        sa.Column("ship_kind_cd", sa.String(length=10), nullable=True),
        sa.Column("ship_kind_nm", sa.String(length=100), nullable=True),
        sa.Column("ship_kind_category", sa.String(length=50), nullable=True),
        sa.Column("entry_purpose_cd", sa.String(length=10), nullable=True),
        sa.Column("entry_purpose_nm", sa.String(length=100), nullable=True),
        sa.Column("origin_port_cd", sa.String(length=20), nullable=True),
        sa.Column("origin_port_nm", sa.String(length=100), nullable=True),
        sa.Column("prev_port_cd", sa.String(length=20), nullable=True),
        sa.Column("prev_port_nm", sa.String(length=100), nullable=True),
        sa.Column("next_port_cd", sa.String(length=20), nullable=True),
        sa.Column("next_port_nm", sa.String(length=100), nullable=True),
        sa.Column("dest_port_cd", sa.String(length=20), nullable=True),
        sa.Column("dest_port_nm", sa.String(length=100), nullable=True),
        sa.Column("departure_sched_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dest_arrival_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_liquid_cargo_vessel", sa.Boolean(), nullable=True),
        sa.Column("is_domestic_voyage", sa.Boolean(), nullable=True),
        *_common_metadata_columns(),
    )
    op.create_index(
        "idx_portmis_vessel_natural_key",
        "portmis_vessel",
        ["callsgn", "entry_year", "entry_count"],
        unique=True,
    )

    op.create_table(
        "tide_obs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("station_id", sa.String(length=20), nullable=True),
        sa.Column("station_name", sa.String(length=100), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("observed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tide_level_cm", sa.Float(), nullable=True),
        sa.Column("wind_dir_deg", sa.Float(), nullable=True),
        sa.Column("wind_speed_ms", sa.Float(), nullable=True),
        sa.Column("gust_ms", sa.Float(), nullable=True),
        sa.Column("air_temp_c", sa.Float(), nullable=True),
        sa.Column("air_pressure_hpa", sa.Float(), nullable=True),
        sa.Column("sea_temp_c", sa.Float(), nullable=True),
        sa.Column("salinity_psu", sa.Float(), nullable=True),
        sa.Column("current_dir_deg", sa.Float(), nullable=True),
        sa.Column("current_speed_cms", sa.Float(), nullable=True),
        *_common_metadata_columns(),
    )
    op.create_index(
        "idx_tide_obs_natural_key", "tide_obs", ["station_id", "observed_at_utc"], unique=True,
    )

    op.create_table(
        "wave_obs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("station_id", sa.String(length=20), nullable=True),
        sa.Column("station_name", sa.String(length=100), nullable=True),
        sa.Column("observed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wave_height_sig_m", sa.Float(), nullable=True),
        sa.Column("wave_height_max_m", sa.Float(), nullable=True),
        sa.Column("wave_height_avg_m", sa.Float(), nullable=True),
        sa.Column("wave_period_s", sa.Float(), nullable=True),
        sa.Column("wave_dir_deg", sa.Float(), nullable=True),
        sa.Column("wind_dir1_deg", sa.Float(), nullable=True),
        sa.Column("wind_speed1_ms", sa.Float(), nullable=True),
        sa.Column("gust1_ms", sa.Float(), nullable=True),
        sa.Column("wind_dir2_deg", sa.Float(), nullable=True),
        sa.Column("wind_speed2_ms", sa.Float(), nullable=True),
        sa.Column("gust2_ms", sa.Float(), nullable=True),
        sa.Column("air_temp_c", sa.Float(), nullable=True),
        sa.Column("sea_temp_c", sa.Float(), nullable=True),
        sa.Column("air_pressure_hpa", sa.Float(), nullable=True),
        sa.Column("humidity_pct", sa.Float(), nullable=True),
        *_common_metadata_columns(),
    )
    op.create_index(
        "idx_wave_obs_natural_key", "wave_obs", ["station_id", "observed_at_utc"], unique=True,
    )

    op.create_table(
        "weather_obs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("station_id", sa.String(length=20), nullable=True),
        sa.Column("station_name", sa.String(length=100), nullable=True),
        sa.Column("agency_code", sa.String(length=20), nullable=True),
        sa.Column("agency_name", sa.String(length=100), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("observed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wind_dir_deg", sa.Float(), nullable=True),
        sa.Column("wind_speed_ms", sa.Float(), nullable=True),
        sa.Column("air_temp_c", sa.Float(), nullable=True),
        sa.Column("humidity_pct", sa.Float(), nullable=True),
        sa.Column("air_pressure_hpa", sa.Float(), nullable=True),
        sa.Column("visibility_m", sa.Float(), nullable=True),
        *_common_metadata_columns(),
    )
    op.create_index(
        "idx_weather_obs_natural_key", "weather_obs", ["station_id", "observed_at_utc"], unique=True,
    )


def downgrade() -> None:
    op.drop_table("weather_obs")
    op.drop_table("wave_obs")
    op.drop_table("tide_obs")
    op.drop_table("portmis_vessel")
    op.drop_table("ais_vessel_static")
    op.drop_table("ais_vessel_position")
