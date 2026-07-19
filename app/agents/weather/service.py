"""기상분석 에이전트 오케스트레이션.

흐름: 기준 시각(as_of) 확정 -> weather_obs/wave_obs에서 그 시각 이전 최신
관측치 조회 -> 결측/오래됨(staleness) 판단 -> rule_engine으로 3(+1)단계 판정.

expected_completion_at이 주어지면 weather_forecast(기상청 getVilageFcst,
data-pipeline이 수집)에서 as_of~expected_completion_at 구간의 예보를 조회해
같은 rule_engine으로 각 시점을 평가하고, 조건부가능 이상으로 악화되는 가장
이른 시점을 찾아 사전 경고를 만든다 (계획서의 "예상 하역완료시간과 단기예보
비교" 요구사항).
"""

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from .data_access import get_forecast_range, get_latest_wave, get_latest_weather
from .rule_engine import MAX_STALENESS, evaluate, severity
from .schemas import (
    ForecastPoint,
    ForecastWarning,
    ObservationFactor,
    WeatherAssessmentRequest,
    WeatherAssessmentResult,
    WorkStatus,
)


def _is_stale(observed_at_utc: datetime | None, as_of: datetime) -> bool:
    if observed_at_utc is None:
        return True
    return (as_of - observed_at_utc) > MAX_STALENESS


async def _build_forecast_warning(
    db: AsyncSession, *, as_of: datetime, expected_completion_at: datetime
) -> ForecastWarning:
    forecast_rows = await get_forecast_range(db, start=as_of, end=expected_completion_at)

    points: list[ForecastPoint] = []
    worst_status = WorkStatus.AVAILABLE
    earliest_deterioration_at: datetime | None = None

    for row in forecast_rows:
        point_status, _ = evaluate(
            wind_speed_ms=row.wind_speed_ms,
            wind_is_stale=False,
            wave_height_m=row.wave_height_m,
            wave_is_stale=False,
        )
        points.append(
            ForecastPoint(
                fcst_at_utc=row.fcst_at_utc,
                status=point_status,
                wind_speed_ms=row.wind_speed_ms,
                wave_height_m=row.wave_height_m,
            )
        )
        if severity(point_status) > severity(worst_status):
            worst_status = point_status
        if (
            earliest_deterioration_at is None
            and severity(point_status) >= severity(WorkStatus.CONDITIONAL)
        ):
            earliest_deterioration_at = row.fcst_at_utc

    return ForecastWarning(
        window_end_utc=expected_completion_at,
        forecast_points_checked=len(points),
        will_deteriorate=earliest_deterioration_at is not None,
        worst_status=worst_status,
        earliest_deterioration_at_utc=earliest_deterioration_at,
        points=points,
    )


async def assess_weather(db: AsyncSession, request: WeatherAssessmentRequest) -> WeatherAssessmentResult:
    as_of = request.as_of or datetime.now(timezone.utc)

    weather_row = await get_latest_weather(db, as_of=as_of)
    wave_row = await get_latest_wave(db, as_of=as_of)

    wind_speed_ms = weather_row.wind_speed_ms if weather_row else None
    wind_observed_at = weather_row.observed_at_utc if weather_row else None
    wind_is_stale = _is_stale(wind_observed_at, as_of)

    wave_height_m = wave_row.wave_height_sig_m if wave_row else None
    wave_observed_at = wave_row.observed_at_utc if wave_row else None
    wave_is_stale = _is_stale(wave_observed_at, as_of)

    status, reasons = evaluate(
        wind_speed_ms=wind_speed_ms,
        wind_is_stale=wind_is_stale,
        wave_height_m=wave_height_m,
        wave_is_stale=wave_is_stale,
    )

    forecast_warning = None
    if request.expected_completion_at is not None:
        forecast_warning = await _build_forecast_warning(
            db, as_of=as_of, expected_completion_at=request.expected_completion_at
        )

    return WeatherAssessmentResult(
        status=status,
        assessed_at_utc=as_of,
        wind=ObservationFactor(
            value=wind_speed_ms,
            unit="m/s",
            observed_at_utc=wind_observed_at,
            station_name=weather_row.station_name if weather_row else None,
            is_stale=wind_is_stale,
        ),
        wave=ObservationFactor(
            value=wave_height_m,
            unit="m",
            observed_at_utc=wave_observed_at,
            station_name=wave_row.station_name if wave_row else None,
            is_stale=wave_is_stale,
        ),
        visibility_m=weather_row.visibility_m if weather_row else None,
        reasons=reasons,
        forecast_warning=forecast_warning,
    )
