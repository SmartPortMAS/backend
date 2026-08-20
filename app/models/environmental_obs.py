from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.common import CollectorMetadataMixin


class TideObs(Base, CollectorMetadataMixin):
    """KHOA 조위 관측. station_id+observed_at_utc 로 upsert.

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 insert만 한다.
    """

    __tablename__ = "tide_obs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    station_id: Mapped[str | None] = mapped_column(String(20))
    station_name: Mapped[str | None] = mapped_column(String(100))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    observed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tide_level_cm: Mapped[float | None] = mapped_column(Float)
    wind_dir_deg: Mapped[float | None] = mapped_column(Float)
    wind_speed_ms: Mapped[float | None] = mapped_column(Float)
    gust_ms: Mapped[float | None] = mapped_column(Float)
    air_temp_c: Mapped[float | None] = mapped_column(Float)
    air_pressure_hpa: Mapped[float | None] = mapped_column(Float)
    sea_temp_c: Mapped[float | None] = mapped_column(Float)
    salinity_psu: Mapped[float | None] = mapped_column(Float)
    current_dir_deg: Mapped[float | None] = mapped_column(Float)
    current_speed_cms: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        Index("idx_tide_obs_natural_key", "station_id", "observed_at_utc", unique=True),
    )


class WaveObs(Base, CollectorMetadataMixin):
    """KMA APIHUB 파고 관측. station_id+observed_at_utc 로 upsert.

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 insert만 한다.
    """

    __tablename__ = "wave_obs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    station_id: Mapped[str | None] = mapped_column(String(20))
    station_name: Mapped[str | None] = mapped_column(String(100))
    observed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wave_height_sig_m: Mapped[float | None] = mapped_column(Float)
    wave_height_max_m: Mapped[float | None] = mapped_column(Float)
    wave_height_avg_m: Mapped[float | None] = mapped_column(Float)
    wave_period_s: Mapped[float | None] = mapped_column(Float)
    wave_dir_deg: Mapped[float | None] = mapped_column(Float)
    wind_dir1_deg: Mapped[float | None] = mapped_column(Float)
    wind_speed1_ms: Mapped[float | None] = mapped_column(Float)
    gust1_ms: Mapped[float | None] = mapped_column(Float)
    wind_dir2_deg: Mapped[float | None] = mapped_column(Float)
    wind_speed2_ms: Mapped[float | None] = mapped_column(Float)
    gust2_ms: Mapped[float | None] = mapped_column(Float)
    air_temp_c: Mapped[float | None] = mapped_column(Float)
    sea_temp_c: Mapped[float | None] = mapped_column(Float)
    air_pressure_hpa: Mapped[float | None] = mapped_column(Float)
    humidity_pct: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        Index("idx_wave_obs_natural_key", "station_id", "observed_at_utc", unique=True),
    )


class WeatherObs(Base, CollectorMetadataMixin):
    """항만기상정보시스템 관측. station_id+observed_at_utc 로 upsert.

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 insert만 한다.
    """

    __tablename__ = "weather_obs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    station_id: Mapped[str | None] = mapped_column(String(20))
    station_name: Mapped[str | None] = mapped_column(String(100))
    agency_code: Mapped[str | None] = mapped_column(String(20))
    agency_name: Mapped[str | None] = mapped_column(String(100))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    observed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wind_dir_deg: Mapped[float | None] = mapped_column(Float)
    wind_speed_ms: Mapped[float | None] = mapped_column(Float)
    air_temp_c: Mapped[float | None] = mapped_column(Float)
    humidity_pct: Mapped[float | None] = mapped_column(Float)
    air_pressure_hpa: Mapped[float | None] = mapped_column(Float)
    visibility_m: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        Index("idx_weather_obs_natural_key", "station_id", "observed_at_utc", unique=True),
    )


class WeatherForecast(Base, CollectorMetadataMixin):
    """기상청 단기예보(getVilageFcst) 울산항 격자(nx=102, ny=84) 예보.

    weather_obs/wave_obs와 달리 관측이 아니라 예측이다. nx+ny+fcst_at_utc로
    upsert되며, 같은 미래 시각에 대한 예보가 재발표될 때마다(하루 8회) 최신 값으로
    덮어써진다 — base_at_utc는 그 예보가 언제 발표됐는지 참고용으로만 남긴다.

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 insert만 한다.
    """

    __tablename__ = "weather_forecast"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    nx: Mapped[int | None] = mapped_column(Integer)
    ny: Mapped[int | None] = mapped_column(Integer)
    base_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fcst_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wind_speed_ms: Mapped[float | None] = mapped_column(Float)
    wave_height_m: Mapped[float | None] = mapped_column(Float)
    air_temp_c: Mapped[float | None] = mapped_column(Float)
    precip_type_code: Mapped[float | None] = mapped_column(Float)
    sky_code: Mapped[float | None] = mapped_column(Float)
    precip_prob_pct: Mapped[float | None] = mapped_column(Float)
    # 강수량(mm) 참고값. 기상청 PCP 원문(구간 텍스트)을 data-pipeline
    # weather_forecast_preprocessor.normalize_pcp_mm()이 근사 정규화한 값이다.
    # rule_engine 판정에는 아직 쓰지 않는다(임계값 출처 미검증) — 참고용으로만 노출한다.
    precip_mm: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        Index("idx_weather_forecast_natural_key", "nx", "ny", "fcst_at_utc", unique=True),
    )
