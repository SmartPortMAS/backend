"""PostgreSQL weather_obs / wave_obs 기준 최신 관측치 조회.

두 테이블 모두 backend(Alembic) 소유 ORM 모델이 있으므로(app.models.environmental_obs)
raw SQL 없이 일반 select()로 조회한다. (scheduling 에이전트의 upa_port_call과 달리
이 테이블들은 backend가 스키마를 직접 관리하는 테이블 — WORK_SUMMARY_2026-07-03의
스키마 소유권 표 참고.)

풍속은 weather_obs(항만기상정보시스템), 파고는 wave_obs(KMA APIHUB)에서 각각
독립적으로 "기준 시각(as_of) 이전 가장 최신 관측 1건"을 가져온다. 두 소스가 서로
다른 관측소/주기이므로 같은 시각일 필요는 없다.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GLOBAL_DEFAULT_BERTH_GROUP, BerthWeatherThreshold
from app.models.environmental_obs import WaveObs, WeatherForecast, WeatherObs

# 울산항 격자좌표 (data-pipeline의 weather_forecast_collector.py와 동일한 값).
# 두 저장소가 별도 배포 단위라 상수를 공유하지 않고 각자 정의한다.
ULSAN_PORT_NX = 102
ULSAN_PORT_NY = 84


async def get_latest_weather(db: AsyncSession, *, as_of: datetime) -> WeatherObs | None:
    stmt = (
        select(WeatherObs)
        .where(WeatherObs.observed_at_utc <= as_of)
        .order_by(WeatherObs.observed_at_utc.desc())
        .limit(1)
    )
    return await db.scalar(stmt)


async def get_latest_wave(db: AsyncSession, *, as_of: datetime) -> WaveObs | None:
    stmt = (
        select(WaveObs)
        .where(WaveObs.observed_at_utc <= as_of)
        .order_by(WaveObs.observed_at_utc.desc())
        .limit(1)
    )
    return await db.scalar(stmt)


async def get_berth_threshold(
    db: AsyncSession, *, berth_group: str | None
) -> BerthWeatherThreshold | None:
    """부두그룹별 기상 임계값 조회. berth_group이 없으면(하위 호환) 전역 폴백 행을 쓴다.

    지정된 berth_group이 테이블에 없으면 None을 그대로 반환한다 — rule_engine이
    이를 '등록 안 된 부두그룹'으로 판단불가(UNKNOWN) 처리한다(임의 기본값으로
    조용히 대체하지 않는다).
    """
    key = berth_group or GLOBAL_DEFAULT_BERTH_GROUP
    return await db.get(BerthWeatherThreshold, key)


async def get_forecast_range(
    db: AsyncSession, *, start: datetime, end: datetime
) -> list[WeatherForecast]:
    """울산항 격자의 start~end(포함) 구간 단기예보를 시각 순으로 조회한다."""
    stmt = (
        select(WeatherForecast)
        .where(
            WeatherForecast.nx == ULSAN_PORT_NX,
            WeatherForecast.ny == ULSAN_PORT_NY,
            WeatherForecast.fcst_at_utc >= start,
            WeatherForecast.fcst_at_utc <= end,
        )
        .order_by(WeatherForecast.fcst_at_utc.asc())
    )
    result = await db.scalars(stmt)
    return list(result.all())
