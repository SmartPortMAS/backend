"""오케스트레이터 최종 종합 의견 프롬프트.

의사결정 자체(선석 선택, 배정불가 재탐색, 기상 중단)는 이미 결정적 로직으로
끝난 뒤에 호출된다 — LLM은 그 결과를 관제사가 읽기 좋은 문장으로 요약할 뿐,
승인/거부 판단을 새로 내리지 않는다.
"""

from datetime import timedelta, timezone

from app.agents.safety.schemas import SafetyAssessmentResult
from app.agents.scheduling.schemas import BerthCandidate
from app.agents.weather.schemas import WeatherAssessmentResult

from .schemas import RejectedCandidate

_KST = timezone(timedelta(hours=9))

SYSTEM_PROMPT = """\
당신은 울산항 액체화물 하역 관제를 보조하는 AI입니다.
스케줄링·안전관제·기상분석 세 에이전트가 이미 결정한 결과가 아래에 주어집니다.
당신은 새로운 판단을 내리지 말고, 주어진 결과만 근거로 관제사가 바로 읽을 수 있는
1~2문단 한국어 요약을 JSON으로 작성하세요. 선석 추천 이유, 위험등급과 핵심 사유,
기상 상태(및 예보 악화 경고가 있다면 그 내용)를 자연스럽게 엮어서 설명하세요.
"""


def _format_weather(weather: WeatherAssessmentResult, *, is_global_fallback: bool = False) -> str:
    label = "현재 상태(전역 기본값 — 이 후보의 배정 실패 사유가 아닐 수 있음)" if is_global_fallback else "현재 상태"
    lines = [f"{label}: {weather.status.value}"]
    if weather.forecast_warning:
        fw = weather.forecast_warning
        if fw.will_deteriorate:
            deteriorate_at_kst = (
                fw.earliest_deterioration_at_utc.astimezone(_KST).strftime("%m월 %d일 %H:%M")
                if fw.earliest_deterioration_at_utc
                else "미상"
            )
            lines.append(
                f"예보 경고: {deteriorate_at_kst}(KST)부터 {fw.worst_status.value} 수준으로 악화 예상"
            )
        else:
            lines.append("예보 경고: 하역 완료 예정 시각까지 악화 없음")
    return "\n".join(lines)


def _format_safety(safety: SafetyAssessmentResult | None) -> str:
    if safety is None:
        return "(안전관제 미실시)"
    return (
        f"위험등급: {safety.risk_level.value}\n"
        f"핵심 유해성: {', '.join(safety.key_hazards) if safety.key_hazards else '없음'}\n"
        f"판단 근거: {safety.reasoning}"
    )


def _format_rejected(rejected: list[RejectedCandidate]) -> str:
    if not rejected:
        return "(없음)"
    return "\n".join(f"  - {r.rank}순위 {r.berth_id}: {r.reason}" for r in rejected)


def build_user_prompt(
    *,
    selected_berth: BerthCandidate | None,
    safety: SafetyAssessmentResult | None,
    weather: WeatherAssessmentResult,
    rejected_candidates: list[RejectedCandidate],
    is_global_fallback_weather: bool = False,
) -> str:
    berth_desc = (
        f"{selected_berth.rank}순위 {selected_berth.wharf_name} "
        f"(수심여유 {selected_berth.draught_margin_m:.1f}m, {selected_berth.occupancy_status.value})"
        if selected_berth
        else "(추천 선석 없음)"
    )
    return f"""\
[최종 추천 선석]
{berth_desc}

[안전관제 결과]
{_format_safety(safety)}

[기상 상태]
{_format_weather(weather, is_global_fallback=is_global_fallback_weather)}

[재탐색 과정에서 탈락한 후보]
{_format_rejected(rejected_candidates)}

위 정보를 종합해 summary를 JSON으로 응답하세요.
"""
