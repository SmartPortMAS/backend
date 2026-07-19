"""기상 임계값 룰 엔진 — LLM 없이 조건문으로만 판단한다 (계획서 18p 명시).

임계값 출처: 계획서 18p "임계값 룰 엔진(풍속 14m/s 초과, 파고 1.5m 초과)으로
작업가능·조건부 가능·불가 세 단계를 판정".

알려진 한계: "조건부 가능" 구간의 정확한 경계값(풍속 10~14m/s, 파고 1.0~1.5m)은
계획서에 수치로 명시되어 있지 않아 이번 구현에서 정한 근사치다. 실제 안전관리자
검토 기준으로 검증된 값이 아니다 (안전관제 에이전트의 rule_engine.py가 이미 가진
것과 같은 성격의 한계 — WORK_SUMMARY_2026-07-12 참고).
"""

from datetime import timedelta

from .schemas import WorkStatus

WIND_BLOCKED_MS = 14.0
WIND_CONDITIONAL_MS = 10.0

WAVE_BLOCKED_M = 1.5
WAVE_CONDITIONAL_M = 1.0

# 이 시간을 넘어선 관측치는 "현재 상황"으로 보지 않고 UNKNOWN(판단불가) 처리한다.
MAX_STALENESS = timedelta(hours=3)

_SEVERITY: dict[WorkStatus, int] = {
    WorkStatus.AVAILABLE: 0,
    WorkStatus.CONDITIONAL: 1,
    WorkStatus.UNKNOWN: 2,
    WorkStatus.BLOCKED: 3,
}


def severity(status: WorkStatus) -> int:
    """상태의 심각도 순위(클수록 심각). 여러 판단 중 가장 심각한 것을 고를 때 사용."""
    return _SEVERITY[status]


def _evaluate_factor(
    value: float | None,
    *,
    is_stale: bool,
    blocked_threshold: float,
    conditional_threshold: float,
    label: str,
    unit: str,
) -> tuple[WorkStatus, str]:
    if value is None or is_stale:
        return WorkStatus.UNKNOWN, f"{label} 관측치 없음 또는 기준 시각 대비 오래됨 - 판단 불가"
    if value > blocked_threshold:
        return WorkStatus.BLOCKED, f"{label} {value}{unit} > {blocked_threshold}{unit} 임계값 초과"
    if value >= conditional_threshold:
        return (
            WorkStatus.CONDITIONAL,
            f"{label} {value}{unit} (조건부 구간 {conditional_threshold}~{blocked_threshold}{unit})",
        )
    return WorkStatus.AVAILABLE, f"{label} {value}{unit}, 정상 범위"


def evaluate(
    *,
    wind_speed_ms: float | None,
    wind_is_stale: bool,
    wave_height_m: float | None,
    wave_is_stale: bool,
) -> tuple[WorkStatus, list[str]]:
    """풍속·파고 각각을 3단계로 평가하고, 더 심각한 쪽을 최종 상태로 채택한다.

    BLOCKED가 가장 심각, 그 다음 UNKNOWN(모르면 가능하다고 하지 않는다),
    CONDITIONAL, AVAILABLE 순.
    """
    wind_status, wind_reason = _evaluate_factor(
        wind_speed_ms,
        is_stale=wind_is_stale,
        blocked_threshold=WIND_BLOCKED_MS,
        conditional_threshold=WIND_CONDITIONAL_MS,
        label="풍속",
        unit="m/s",
    )
    wave_status, wave_reason = _evaluate_factor(
        wave_height_m,
        is_stale=wave_is_stale,
        blocked_threshold=WAVE_BLOCKED_M,
        conditional_threshold=WAVE_CONDITIONAL_M,
        label="파고",
        unit="m",
    )

    final_status = max((wind_status, wave_status), key=lambda s: _SEVERITY[s])
    return final_status, [wind_reason, wave_reason]
