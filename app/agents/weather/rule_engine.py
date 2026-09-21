"""기상 임계값 룰 엔진 — LLM 없이 조건문으로만 판단한다 (계획서 18p 명시).

온산 MVP(feature/onsan-mvp) 이식: 전역 단일 임계값 대신 부두그룹별 임계값
테이블(berth_weather_threshold)을 조회해, 정상 -> 하역중단 -> 이안 -> 호스분리
3단계로 에스컬레이션되는 판정을 한다(팀원의 onsan_mvp/scripts/weather_berth_agent.py
로직을 그대로 옮긴 것 — assess_berth_weather의 _level_for_metric/_max_level와
동일 구조).

기존 코드와의 차이(의도적으로 보존한 부분): 팀원 원본 스크립트는 관측값이 None이면
"정상"으로 기본 처리하지만, 이 프로젝트는 처음부터 "모르면 가능하다고 하지 않는다"
원칙(안전관제 에이전트 rule_engine.py의 floor 설계와 동일 철학)을 지켜왔다. 그래서
값이 없거나(None) 오래된 관측(is_stale)이면 그 항목은 UNKNOWN으로 취급하고,
UNKNOWN이 하나라도 있으면 전체 판정을 UNKNOWN으로 확정한다(severity상 최고 심각도).
"""

from datetime import timedelta
from typing import Protocol

from .schemas import WorkStatus

# 이 시간을 넘어선 관측치는 "현재 상황"으로 보지 않고 UNKNOWN(판단불가) 처리한다.
MAX_STALENESS = timedelta(hours=3)

# 정성 조건(대기정체/심한뇌우/태풍경로 등)이 발효되면 최소 이 단계 이상으로 올린다.
EXTRA_CONDITION_MIN_LEVEL = WorkStatus.STOP

# 강수량 임계값(mm/h) — 풍속·파고와 달리 부두그룹별 값이 아니라 전 항만 공통
# 법적/행정 기준이라 berth_weather_threshold에 넣지 않고 여기 상수로 둔다.
# 출처(NotebookLM 문서검색 경유, 2026-07-25 대화 — 원문 페이지 직접 대조는 안 됨,
# 추후 1차 문서로 재확인 권장):
#   PRECIP_STOP: 산업안전보건기준에 관한 규칙 제383조 제2호(철골작업 중지 기준,
#                시간당 1mm 이상 — 항만 하역에는 준용 적용)
#   PRECIP_UNBERTH: '기상 정보관리' 지침 기상특보 발표기준표, "폭풍우주의보"
#                   (비 20mm/h 이상 동반 시)
#   PRECIP_DISCONNECT: 같은 기준표, "폭풍우경보"(비 30mm/h 이상 동반 시)
# 실시간 관측(weather_obs)에는 강수량 필드 자체가 없다(항만기상정보시스템 API
# 미제공). 예보(weather_forecast.precip_mm)는 이 값으로 그대로 판정하고, 현재
# 상태는 관제사 육안 확인(WeatherAssessmentRequest.precip_observed)이 True일 때
# PRECIP_STOP_MM으로 채워 넣는다(service.py) — 파고와 달리 강수 유무는 육안
# 판단이 가능하다는 전제. evaluate()의 precip_mm 인자를 아예 생략하면(둘 다
# 없는 경우) 이 판정을 건너뛴다.
PRECIP_STOP_MM = 1.0
PRECIP_UNBERTH_MM = 20.0
PRECIP_DISCONNECT_MM = 30.0

_SEVERITY: dict[WorkStatus, int] = {
    WorkStatus.NORMAL: 0,
    WorkStatus.STOP: 1,
    WorkStatus.UNBERTH: 2,
    WorkStatus.DISCONNECT: 3,
    WorkStatus.UNKNOWN: 4,
}


def severity(status: WorkStatus) -> int:
    """상태의 심각도 순위(클수록 심각). 여러 판단 중 가장 심각한 것을 고를 때 사용.

    UNKNOWN을 가장 높은 순위에 둔 것은 팀원 원본 설계에는 없던 부분이다 — "확인된
    최악의 물리 상태(호스분리)"보다 "아예 확인이 안 되는 상태"를 더 보수적으로
    다루는 게 이 프로젝트의 기존 fail-safe 원칙과 일치한다.
    """
    return _SEVERITY[status]


def _max_status(*statuses: WorkStatus) -> WorkStatus:
    return max(statuses, key=severity)


class BerthWeatherThresholdRow(Protocol):
    """data_access.get_berth_threshold가 반환하는 행이 갖춰야 할 최소 인터페이스.

    ORM 모델(app.models.BerthWeatherThreshold)을 그대로 넘겨도 되고, 테스트에서는
    같은 속성을 가진 어떤 객체를 넘겨도 된다(구조적 타이핑).
    """

    stop_wind_ms: float | None
    stop_wave_m: float | None
    unberth_wind_ms: float | None
    unberth_wave_m: float | None
    disconnect_wind_ms: float | None
    disconnect_wave_m: float | None
    extra_conditions: str | None


def _level_for_metric(
    value: float | None,
    *,
    is_stale: bool,
    stop: float | None,
    unberth: float | None,
    disconnect: float | None,
) -> tuple[WorkStatus, float | None]:
    """단일 지표(풍속 또는 파고)가 어느 단계를 넘겼는지. (상태, 넘긴 임계값) 반환.

    임계값 자체가 None(해당 부두그룹에 그 지표 기준이 없음)이면 그 단계는 건너뛴다
    (팀원 원본과 동일 — 예: S-Oil 그룹은 파고 기준이 아예 없음).

    2026-08-21 수정 — "이 지표 기준 자체가 없는 부두"에서는 관측 결측·오래됨이
    UNKNOWN으로 격상시키지 않는다. 예전 코드는 value가 None/stale이면 임계값
    유무와 무관하게 무조건 UNKNOWN을 반환해, 파고 기준이 아예 없는 S-Oil
    부두군이 "풍속은 명백히 정상인데 파고 관측이 없다"는 이유만으로 전체
    판정이 판단불가로 격상되는 결함이 있었다(실제 재현 확인). "모르면 진행
    하지 않는다" 원칙은 "이 지표가 적용되는데 모를 때"에만 맞는 얘기고, 애초에
    적용되지 않는 지표의 결측은 판정과 무관해야 한다.
    """
    if stop is None and unberth is None and disconnect is None:
        return WorkStatus.NORMAL, None
    if value is None or is_stale:
        return WorkStatus.UNKNOWN, None
    if disconnect is not None and value >= disconnect:
        return WorkStatus.DISCONNECT, disconnect
    if unberth is not None and value >= unberth:
        return WorkStatus.UNBERTH, unberth
    if stop is not None and value >= stop:
        return WorkStatus.STOP, stop
    return WorkStatus.NORMAL, None


def wave_applies_to(wharf_name: str | None) -> bool:
    """이 계선시설에 **외해 파고 관측**을 적용해도 되는가.

    [2026-09-21, D2 ② — 9/17 회의 §6 "외해 부이 파고를 항내에 대입 13%p"]

    우리가 가진 파고는 기상청 부이 22189 관측 하나다. **외해 값이다.** 방파제
    안쪽 부두의 파고가 아니다. 그걸 항내 부두에 그대로 대입하면 방파제가 막아
    주는 너울까지 부두에 친 것으로 계산된다.

    실제로 그랬다. 2026-09-21 라이브 판정 26척 중 24척이 부적합이었고, 근거를
    펼쳐 보니 전부 같은 모양이었다:

        풍속 10.1m/s < 14.0m/s(중단 임계) - 정상
        파고 2.0m >= 1.5m -> 하역중단          ← 외해 부이 값

    SK6부두·4부두처럼 항내 깊숙한 부두까지 이 한 줄로 막혔다. 바람은 멀쩡한데
    파고 하나로 항만 전체가 멈춘 셈이다.

    부이 계류시설은 다르다 — 방파제 밖에 떠 있으므로 외해 파고가 곧 그 자리의
    파고다. 그래서 이름에 '부이'가 들어간 시설에만 적용한다.

    판별을 이름으로 하는 건 투박하지만 근거가 있다. 부이 계선시설은 마스터에서
    전부 '…부이', 'S-Oil부이', 'SK부이 02' 처럼 표기되고, 부두는 '…부두'로
    표기된다. 오경보 백테스트도 같은 규칙을 쓴다
    (`backtest_false_alarm.py`: `is_buoy = "부이" in b["name"]`) — 두 곳이 같은
    규칙을 써야 백테스트 숫자가 실제 판정의 숫자가 된다.

    한계는 분명히 둔다. 이건 "항내 부두는 파고가 안전하다"는 뜻이 **아니다.**
    "항내 부두의 파고를 우리가 모른다"는 뜻이다. 부두별 파고 관측이 생기면
    그때 다시 축으로 넣어야 한다.
    """
    return "부이" in (wharf_name or "")


def evaluate(
    *,
    wind_speed_ms: float | None,
    wind_is_stale: bool,
    wave_height_m: float | None,
    wave_is_stale: bool,
    threshold: BerthWeatherThresholdRow | None,
    extra_condition_active: bool = False,
    precip_mm: float | None = None,
    wave_applies: bool = True,
) -> tuple[WorkStatus, list[str]]:
    """풍속/파고(+선택적으로 강수량)를 임계값과 비교해 심각도 최대 단계를 반환.

    threshold가 None이면(berth_group이 임계값 테이블에 등록되지 않음) 판단불가로
    fail-safe 처리한다 — 등록 안 된 부두그룹을 임의 기본값으로 판단하지 않는다.

    precip_mm은 풍속/파고와 취급이 다르다 — 지금은 실시간 관측(weather_obs)에
    강수량 데이터 자체가 없어서(항만기상정보시스템 API 미제공), "값이 없다"가
    "관측이 안 됐다/오래됐다"(fail-safe UNKNOWN 대상)가 아니라 "이 판단에는 강수량
    신호 자체가 없다"는 뜻이다. 그래서 precip_mm을 생략하면(기본값 None) 판정에
    아예 관여하지 않는다 — wind/wave처럼 None이라고 UNKNOWN으로 격상시키지
    않는다. 단기예보(weather_forecast.precip_mm)가 있을 때만 호출부(service.py의
    _build_forecast_warning)가 값을 채워 넘긴다.
    """
    if threshold is None:
        return WorkStatus.UNKNOWN, ["부두그룹 임계값 미등록. berth_weather_threshold 확인 필요"]

    wind_status, wind_hit = _level_for_metric(
        wind_speed_ms,
        is_stale=wind_is_stale,
        stop=threshold.stop_wind_ms,
        unberth=threshold.unberth_wind_ms,
        disconnect=threshold.disconnect_wind_ms,
    )
    if wave_applies:
        wave_status, wave_hit = _level_for_metric(
            wave_height_m,
            is_stale=wave_is_stale,
            stop=threshold.stop_wave_m,
            unberth=threshold.unberth_wave_m,
            disconnect=threshold.disconnect_wave_m,
        )
    else:
        # 적용 안 함 ≠ 판단불가. 이 구분이 핵심이다 — UNKNOWN 으로 두면
        # '모르면 닫는다' 원칙에 걸려 항내 부두가 전부 잠긴다. 관측이 없는 게
        # 아니라 **이 자리에 쓸 수 없는 관측**이라는 뜻이다(wave_applies_to 주석).
        wave_status, wave_hit = WorkStatus.NORMAL, None

    status = _max_status(wind_status, wave_status)
    reasons: list[str] = []

    if wind_status is WorkStatus.UNKNOWN:
        reasons.append("풍속 관측치 없음 또는 기준 시각 대비 오래됨 - 판단 불가")
    elif wind_status is not WorkStatus.NORMAL:
        reasons.append(f"풍속 {wind_speed_ms}m/s >= {wind_hit}m/s -> {wind_status.value}")
    elif threshold.stop_wind_ms is not None:
        # 정상 판정도 실측값을 남긴다 — "모든 임계값 이내, 정상" 한 줄뿐이면
        # 관제사가 실제 풍속을 알 수 없어 근거 없이 정상이라고만 우기는 것처럼
        # 보인다(2026-08-20 지적). 초과 케이스와 같은 형식으로 값을 보여준다.
        reasons.append(f"풍속 {wind_speed_ms}m/s < {threshold.stop_wind_ms}m/s(중단 임계) - 정상")

    if not wave_applies:
        # 값을 숨기지 않는다 — 관측치는 보여주되 판정에 안 썼다고 밝힌다.
        # 관제사가 "그럼 파고는 본 거냐 안 본 거냐"를 묻지 않아도 되게.
        reasons.append(
            f"파고 {wave_height_m}m는 외해 부이(22189) 관측이라 이 항내 계선시설 판정에 "
            "적용하지 않음 - 참고값"
            if wave_height_m is not None
            else "파고는 외해 부이 관측이라 이 항내 계선시설 판정에 적용하지 않음"
        )
    elif wave_status is WorkStatus.UNKNOWN:
        reasons.append("파고 관측치 없음 또는 기준 시각 대비 오래됨 - 판단 불가")
    elif wave_status is not WorkStatus.NORMAL:
        reasons.append(f"파고 {wave_height_m}m >= {wave_hit}m -> {wave_status.value}")
    elif threshold.stop_wave_m is not None:
        reasons.append(f"파고 {wave_height_m}m < {threshold.stop_wave_m}m(중단 임계) - 정상")

    if precip_mm is not None:
        precip_status, precip_hit = _level_for_metric(
            precip_mm,
            is_stale=False,
            stop=PRECIP_STOP_MM,
            unberth=PRECIP_UNBERTH_MM,
            disconnect=PRECIP_DISCONNECT_MM,
        )
        if precip_status is not WorkStatus.NORMAL:
            status = _max_status(status, precip_status)
            reasons.append(f"강수량 {precip_mm}mm/h >= {precip_hit}mm/h -> {precip_status.value}")

    if extra_condition_active and status is not WorkStatus.UNKNOWN:
        status = _max_status(status, EXTRA_CONDITION_MIN_LEVEL)
        cond = threshold.extra_conditions or "대기정체/심한뇌우/태풍경로"
        reasons.append(f"정성조건 발효({cond}) -> 최소 {EXTRA_CONDITION_MIN_LEVEL.value}")

    if not reasons:
        reasons.append("모든 임계값 이내, 정상")

    return status, reasons
