"""PostgreSQL weather_obs / wave_obs 기준 최신 관측치 조회.

두 테이블 모두 backend(Alembic) 소유 ORM 모델이 있으므로(app.models.environmental_obs)
raw SQL 없이 일반 select()로 조회한다. (scheduling 에이전트의 upa_port_call과 달리
이 테이블들은 backend가 스키마를 직접 관리하는 테이블 — WORK_SUMMARY_2026-07-03의
스키마 소유권 표 참고.)

풍속은 weather_obs(항만기상정보시스템), 파고는 wave_obs(KMA APIHUB)에서 각각
독립적으로 "기준 시각(as_of) 이전 가장 최신 관측 1건"을 가져온다. 두 소스가 서로
다른 관측소/주기이므로 같은 시각일 필요는 없다.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GLOBAL_DEFAULT_BERTH_GROUP, BerthWeatherThreshold
from app.models.environmental_obs import TideObs, WaveObs, WeatherForecast, WeatherObs

# 울산항 격자좌표 (data-pipeline의 weather_forecast_collector.py와 동일한 값).
# 두 저장소가 별도 배포 단위라 상수를 공유하지 않고 각자 정의한다.
ULSAN_PORT_NX = 102
ULSAN_PORT_NY = 84


@dataclass(frozen=True)
class WindObservation:
    """풍속 관측 1건. 어느 테이블에서 왔든 같은 모양으로 다룬다.

    필드명은 기존 WeatherObs 와 같게 둬서 호출부(service.assess_weather)가
    그대로 쓰도록 했다.

    visibility_m 만 다른 관측에서 온다 — 시정은 항만기상에만 있고(조위·부이는
    측정하지 않는다) 풍속과 결측 시점도 다르므로, 풍속과 별개로 "값이 있는
    최신 행"을 찾아 채운다. 시각이 서로 다를 수 있다는 뜻이라 판정에서 시정은
    보조 지표로만 쓴다.
    """

    wind_speed_ms: float | None
    wind_dir_deg: float | None
    observed_at_utc: datetime | None
    source: str
    station_name: str | None
    visibility_m: float | None = None


async def get_latest_weather(db: AsyncSession, *, as_of: datetime) -> WindObservation | None:
    """as_of 이전 풍속 관측 중 가장 최근 1건. 세 소스를 함께 본다.

    [왜 weather_obs 최신 행 하나가 아닌가 — 2026-08-15]
    항만기상(MMAF)이 관측값 칸이 빈 행을 시각만 채워 매시간 보낸다. 실측상 하루
    24행 중 풍속이 들어있는 행은 0~6건뿐이고, 최신 행은 거의 항상 NULL 이다.
    그 행을 집으면 wind_speed_ms 가 None 이 되어 rule_engine 이 전 선석을
    "판단불가"로 떨어뜨린다 — 실제로 관측이 있는데도 판정을 못 하는 상태가 된다.

    풍속은 조위관측소(KHOA)·부이(KMA)도 함께 보내고 그쪽이 훨씬 촘촘하다
    (실측: 조위 1,803행 전부 / 부이 250행 전부 / 항만기상 248행 중 33건).
    셋을 합쳐 그중 가장 최근 관측을 쓴다.

    같은 정의가 mart.weather_now 에도 있다(대시보드 표시용). 그 뷰를 그대로
    읽지 않는 이유는 as_of — 뷰는 "지금"만 알고, 이 함수는 과거 시점 판정
    (request.as_of)을 지원해야 한다. 정의가 바뀌면 양쪽을 같이 고쳐야 한다.
    """
    candidates = [
        select(
            WeatherObs.wind_speed_ms.label("wind_speed_ms"),
            WeatherObs.wind_dir_deg.label("wind_dir_deg"),
            WeatherObs.observed_at_utc.label("observed_at_utc"),
            literal("MMAF_PORT").label("source"),
            WeatherObs.station_name.label("station_name"),
        ).where(WeatherObs.wind_speed_ms.is_not(None)),
        select(
            TideObs.wind_speed_ms,
            TideObs.wind_dir_deg,
            TideObs.observed_at_utc,
            literal("KHOA_TIDE"),
            TideObs.station_name,
        ).where(TideObs.wind_speed_ms.is_not(None)),
        # 부이는 풍향·풍속 센서가 2조이고 1번이 주센서다
        select(
            WaveObs.wind_speed1_ms,
            WaveObs.wind_dir1_deg,
            WaveObs.observed_at_utc,
            literal("KMA_BUOY"),
            WaveObs.station_name,
        ).where(WaveObs.wind_speed1_ms.is_not(None)),
    ]
    unioned = union_all(
        *[c.where(c.selected_columns.observed_at_utc <= as_of) for c in candidates]
    ).subquery()

    stmt = select(unioned).order_by(unioned.c.observed_at_utc.desc()).limit(1)
    row = (await db.execute(stmt)).mappings().first()
    if row is None:
        return None

    # 시정은 항만기상에만 있고 풍속과 결측 시점이 달라 따로 찾는다
    visibility = await db.scalar(
        select(WeatherObs.visibility_m)
        .where(WeatherObs.visibility_m.is_not(None), WeatherObs.observed_at_utc <= as_of)
        .order_by(WeatherObs.observed_at_utc.desc())
        .limit(1)
    )

    return WindObservation(
        wind_speed_ms=row["wind_speed_ms"],
        wind_dir_deg=row["wind_dir_deg"],
        observed_at_utc=row["observed_at_utc"],
        source=row["source"],
        station_name=row["station_name"],
        visibility_m=visibility,
    )


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
