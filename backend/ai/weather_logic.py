import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# ─── 온산 MVP 선석별 기상 모듈 배선 (onsan_mvp/scripts 재사용, 재구현 금지) ───
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ONSAN_SCRIPTS = os.path.join(_REPO_ROOT, "onsan_mvp", "scripts")
if _ONSAN_SCRIPTS not in sys.path:
    sys.path.insert(0, _ONSAN_SCRIPTS)

from weather_berth_agent import (  # noqa: E402
    assess_berth_weather,
    load_thresholds,
    LEVEL_NORMAL,
    LEVEL_STOP,
)

_thresholds_cache: Optional[dict] = None


def _get_thresholds() -> dict:
    """berth_weather_thresholds.csv 를 1회 로드 후 캐시.

    경로는 BERTH_WEATHER_THRESHOLDS_CSV 환경변수로 재지정 가능.
    """
    global _thresholds_cache
    if _thresholds_cache is None:
        csv_path = os.environ.get("BERTH_WEATHER_THRESHOLDS_CSV")
        _thresholds_cache = load_thresholds(csv_path) if csv_path else load_thresholds()
        logger.info("선석별 기상 임계 로드: %d개 선석군", len(_thresholds_cache))
    return _thresholds_cache


def list_berth_groups() -> list:
    """등록된 선석군 목록 (프론트 드롭다운용)."""
    return list(_get_thresholds().keys())


def assess_weather(
    berth_group: str,
    wind_speed: Optional[float] = None,
    wave_height: Optional[float] = None,
    visibility: Optional[float] = None,
    extra_condition_active: bool = False,
    is_stale: bool = False,
    forecast_wind: Optional[float] = None,
    forecast_wave: Optional[float] = None,
) -> dict:
    """선석별 3단계 기상 판정 (정상/하역중단/이안/호스분리 + 판단불가 fail-safe).

    - is_stale=True 면 관측이 오래된 것이므로 판단하지 않고 '판단불가' 반환 (fail-safe 유지)
    - visibility 는 선석별 임계표에 없으므로 기존 전역 기준(0.5km 미만 불가)을 유지해
      0.5km 미만이면 최소 '하역중단'으로 격상
    - forecast_wind/forecast_wave 가 해당 선석 임계를 넘기면 forecast_warning 세팅 (현재 status 는 격상하지 않음)
    """
    if is_stale:
        return {
            "berth_group": berth_group,
            "status": "판단불가",
            "reasons": ["관측값이 최신이 아님(is_stale). fail-safe로 판단 보류"],
            "wind": {"value": wind_speed, "unit": "m/s"},
            "wave": {"value": wave_height, "unit": "m"},
            "visibility": {"value": visibility, "unit": "km"},
            "thresholds_used": None,
            "forecast_warning": None,
            "is_stale": True,
        }

    thresholds = _get_thresholds()
    verdict = assess_berth_weather(
        berth_group, wind_speed, wave_height, thresholds,
        extra_condition_active=extra_condition_active,
    )
    result = verdict.as_dict()
    status = result.pop("level")
    reasons = result["reasons"]

    if visibility is not None and visibility < 0.5:
        if status == LEVEL_NORMAL:
            status = LEVEL_STOP
        reasons.append(f"시정 {visibility} km < 0.5 km -> 최소 {LEVEL_STOP} (전역 기준 유지)")

    forecast_warning = None
    if forecast_wind is not None or forecast_wave is not None:
        fv = assess_berth_weather(berth_group, forecast_wind, forecast_wave, thresholds)
        if fv.level not in (LEVEL_NORMAL, "판단불가"):
            forecast_warning = f"예보값 기준 '{fv.level}' 도달 예상: " + "; ".join(fv.reasons)

    result.update({
        "status": status,
        "visibility": {"value": visibility, "unit": "km"},
        "forecast_warning": forecast_warning,
        "is_stale": False,
    })
    return result


# ─── 레거시 단일 임계 (대시보드 호환용. 신규 판정은 assess_weather 사용) ───

def check_weather_conditions(wind_speed: float, wave_height: float, visibility: float):
    """
    기상청 API 연동을 가정한 Rule-based 기상 판정 로직
    - 풍속 14m/s 초과 OR 파고 1.5m 초과 OR 시정 0.5km 미만 -> 불가
    - 풍속 10m/s 초과 OR 파고 1.0m 초과 OR 시정 1.0km 미만 -> 조건부
    - 그 외 -> 가능
    """
    if wind_speed > 14.0 or wave_height > 1.5 or visibility < 0.5:
        return "불가"
    elif wind_speed > 10.0 or wave_height > 1.0 or visibility < 1.0:
        return "조건부"
    else:
        return "가능"

def get_weather_detail(wind_speed: float = 8.2, wave_height: float = 1.4, visibility: float = 5.0):
    status = check_weather_conditions(wind_speed, wave_height, visibility)
    return {
        "status": status,
        "wind_speed": wind_speed,
        "wave_height": wave_height,
        "visibility": visibility,
        "warning": "하역 중단 권고" if status == "불가" else ("안전 유의" if status == "조건부" else "특이사항 없음")
    }
