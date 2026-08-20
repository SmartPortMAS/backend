"""오케스트레이터 최종 종합 의견 프롬프트.

의사결정 자체(선석 선택, 배정불가 재탐색, 기상 중단)는 이미 결정적 로직으로
끝난 뒤에 호출된다 — LLM은 그 결과를 관제사가 읽기 좋은 문장으로 요약할 뿐,
승인/거부 판단을 새로 내리지 않는다.
"""

from datetime import timedelta, timezone

from app.agents.safety.schemas import SafetyAssessmentResult
from app.agents.scheduling.schemas import BerthCandidate, VesselSpec
from app.agents.weather.schemas import WeatherAssessmentResult

from .schemas import RejectedCandidate

_KST = timezone(timedelta(hours=9))

SYSTEM_PROMPT = """\
당신은 울산항 액체화물 하역 관제를 보조하는 AI입니다.
스케줄링·안전관제·기상분석 세 에이전트가 이미 결정한 결과가 아래에 주어집니다.
당신은 새로운 판단을 내리지 말고, 주어진 결과만 근거로 아래 두 가지를 JSON으로 작성하세요.

1. summary — 관제사가 바로 읽을 수 있는 1~2문단 한국어 종합 의견. 선석 추천 이유,
   위험등급과 핵심 사유, 기상 상태(및 예보 악화 경고가 있다면 그 내용)를 자연스럽게
   엮어서 설명하세요.

2. berth_match_summary — 선석 배정현황 화면에서 "이 선석이 왜 이 배와 맞는가"만
   보여줄 별도의 짧은 한 문장. [최종 추천 선석]·[선박 정보]·[배정 경로]·
   [재탐색 과정에서 탈락한 후보]·[기상 상태]를 전부 활용해서, 수심 대비 흘수가
   얼마나 여유 있는지뿐 아니라 전용/대체 중 어느 경로로 확정됐는지, 대체라면
   원래 선석은 왜 안 됐는지, 기상 임계값을 통과했는지까지 근거를 다양하게
   엮으세요. **수심 얘기 하나로 문장을 끝내지 마세요** — 주어진 항목 중 최소
   2가지 이상을 언급해야 합니다. **[안전관제 결과]는 이 문장에 절대 쓰지 마세요**
   — 화학물질 이름·위험등급·유해성 언급이 하나라도 들어가면 안 됩니다. 그
   정보는 summary에만 담깁니다.
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


def _format_vessel(vessel: VesselSpec | None) -> str:
    if vessel is None:
        return "(정보 없음)"
    parts = [f"흘수 {vessel.draught_m}m"]
    if vessel.dwt_t is not None:
        parts.append(f"DWT {vessel.dwt_t:,.0f}t")
    if vessel.name_hint:
        parts.append(vessel.name_hint)
    return " · ".join(parts)


def build_user_prompt(
    *,
    selected_berth: BerthCandidate | None,
    safety: SafetyAssessmentResult | None,
    weather: WeatherAssessmentResult,
    rejected_candidates: list[RejectedCandidate],
    vessel: VesselSpec | None = None,
    assignment_trace: list[str] | None = None,
    is_global_fallback_weather: bool = False,
) -> str:
    # berth_match_summary가 "수심 얘기만" 나오던 원인이 여기였다(2026-08-19 지적) —
    # 예전엔 이 함수가 draught_margin_m 하나만 문장으로 만들어 넘겼다. LLM은 준
    # 것만 쓸 수 있으므로, 선석 스펙 전체(수심·정원·기상그룹)와 선박 스펙,
    # 전용/대체 판단 경로까지 전부 명시적으로 넘긴다.
    if selected_berth:
        berth_lines = [
            f"{selected_berth.rank}순위 {selected_berth.wharf_name}"
            f"({selected_berth.port_name or '항만 미상'})",
            f"수심 {selected_berth.depth_m}m · 흘수여유 {selected_berth.draught_margin_m:.1f}m"
            f" · 배정 전 상태 {selected_berth.occupancy_status.value}",
        ]
        if selected_berth.berth_group:
            berth_lines.append(f"기상 임계값 그룹: {selected_berth.berth_group}")
        if selected_berth.unload_capacity is not None:
            berth_lines.append(f"하역능력: {selected_berth.unload_capacity}")
        berth_desc = "\n".join(berth_lines)
    else:
        berth_desc = "(추천 선석 없음)"

    trace_desc = " → ".join(assignment_trace) if assignment_trace else "(단일 후보, 재탐색 없음)"

    return f"""\
[최종 추천 선석]
{berth_desc}

[선박 정보]
{_format_vessel(vessel)}

[배정 경로]
{trace_desc}

[안전관제 결과]
{_format_safety(safety)}

[기상 상태]
{_format_weather(weather, is_global_fallback=is_global_fallback_weather)}

[재탐색 과정에서 탈락한 후보]
{_format_rejected(rejected_candidates)}

위 정보를 종합해 summary와 berth_match_summary를 JSON으로 응답하세요.
berth_match_summary에는 [안전관제 결과] 내용을 쓰지 마세요.
"""
