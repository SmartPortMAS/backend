"""오케스트레이터 — 정해진 선석 하나가 지금 이 배·화물에 맞는가(검증모드 전용).

흐름 (감독자 — 27번 설계안):
1. 선석 도구(check_berth): 배정된 선석의 가용수심·흘수 여유·이웃 화물. 맞지 않으면
   종료 — 근거 부족이면 판정불가, 실제 부적합이면 대체 선석 **제안**을 붙인다.
2. 기상 도구(check_weather): 그 선석의 임계값으로 판정. 정상이 아니면 종료
   (관측 없음 → 판정불가, 기준 초과 → 기상 사유 + 대체 제안).
3. 혼재 도구(check_segregation): 이웃 화물과의 규칙 하한. 배정불가면 부적합 + 대체 제안.
4. 교차 확인(_cross_check): 체류 중 예보 악화 등 조건을 붙인다.
5. 적합/부적합일 때만 LLM 이 관제사용 문장을 쓴다(등급은 바꾸지 못한다).
도구마다 의견(Opinion: 등급 · 확인한 것 · 못 본 것)을 모아 결과에 싣는다.

[2026-09-27] 탐색모드(선석 top-3 를 새로 고르고, 점유면 대체 선석 → 정박지 대기로
재탐색)를 걷어냈다(27번 설계안 D단계, 결정 7-3). 우리는 배정하지 않는다 — 실측(9/27):
종합 판정 콘솔이 탐색모드로 43척 중 42척에 실제 접안 선석 검증과 다른 답(다른 선석
추천·'적합 선석 없음')을 보여 주고 있었다. 정박지는 위치로 관찰만 한다(/dashboard/anchorages).
"""

from datetime import timedelta, timezone

from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import tools
from app.agents.safety.schemas import RiskLevel, SafetyAssessmentResult
from app.agents.scheduling.schemas import BerthCandidate, OccupancyStatus, VesselSpec
from app.agents.weather.schemas import WeatherAssessmentResult, WorkStatus
from app.llm.base import LLMClient

from .prompt import SYSTEM_PROMPT, build_user_prompt
from .schemas import LLMSummary, OrchestratorRequest, OrchestratorResult, OverallDecision, RejectedCandidate

_KST = timezone(timedelta(hours=9))


def _cross_check(weather: WeatherAssessmentResult) -> tuple[list[str], str]:
    """교차 확인 — 두 의견을 함께 봐야 나오는 결론. (조건 문장들, 기록 비교용 키).

    27번 설계안 3-2 의 규칙 표. 규칙을 늘릴 때는 여기에 한 줄씩 더하고 테스트를 둔다.

    ① 선석 적합 + 지금 기상 정상 + 체류 중 예보 악화 → 등급은 적합 그대로, 악화 예상
       시각을 조건으로 붙인다(결정 7-1: 새 등급을 만들지 않는다). 그 시각에 실제 관측이
       기준을 넘으면 기상 게이트(3초 주기)가 하역을 멈춘다. 25번 점검 C4 — 6시간 뒤
       풍속 25m/s 예보를 넣어도 아무 표시 없이 '적합'이던 문제.
    """
    fw = weather.forecast_warning
    if (
        weather.status is WorkStatus.NORMAL
        and fw is not None
        and fw.will_deteriorate
        and fw.earliest_deterioration_at_utc is not None
    ):
        at = fw.earliest_deterioration_at_utc.astimezone(_KST)
        return (
            [f"체류 중 {at:%m/%d %H:%M} 부터 '{fw.worst_status.value}' 예보 — "
             f"그 시각 관측이 기준을 넘으면 기상 게이트가 하역을 멈춥니다"],
            f"예보:{fw.worst_status.value}",
        )
    return [], ""


async def _llm_summary(
    llm_client: LLMClient,
    *,
    selected_berth: BerthCandidate | None,
    safety: SafetyAssessmentResult | None,
    weather: WeatherAssessmentResult,
    rejected: list[RejectedCandidate],
    vessel: VesselSpec | None = None,
    assignment_trace: list[str] | None = None,
    is_global_fallback_weather: bool = False,
) -> LLMSummary:
    user_prompt = build_user_prompt(
        selected_berth=selected_berth,
        safety=safety,
        weather=weather,
        rejected_candidates=rejected,
        vessel=vessel,
        assignment_trace=assignment_trace,
        is_global_fallback_weather=is_global_fallback_weather,
    )
    return await llm_client.generate_structured(
        system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt, schema=LLMSummary
    )


def _no_candidate_summary(reason: str) -> str:
    return f"{reason} 안전하게 배정을 진행할 수 없어 관제사의 직접 확인이 필요합니다."


async def orchestrate(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    request: OrchestratorRequest,
) -> OrchestratorResult:
    # 전역 폴백 판정(__GLOBAL_DEFAULT__ 임계값). 선석을 확인하지 못했거나 혼재로 부적합인
    # 응답의 weather_assessment 를 채우는 용도다 — 통과/차단은 아래 선석별 임계값으로 한다.
    weather_result = await tools.check_weather(
        db, window_end=request.window_end, as_of=request.weather_as_of,
    )
    opinions: list[tools.Opinion] = []

    async def _alternatives() -> tools.Alternatives:
        # 배정이 아니라 제안이다(회의 §3 조치안). 지금 선석은 뺀다.
        return await tools.suggest_alternatives(
            db, neo4j_driver,
            cargo=request.cargo, cargos=request.cargos, vessel=request.vessel,
            window_start=request.window_start, window_end=request.window_end,
            exclude_wharf_name=request.assigned_wharf_name,
            draught_margin_m=request.draught_margin_m,
        )

    # ── 1. 선석 ────────────────────────────────────────────────────────────
    berth_check = await tools.check_berth(
        db,
        neo4j_driver,
        wharf_name=request.assigned_wharf_name,
        vessel=request.vessel,
        window_start=request.window_start,
        window_end=request.window_end,
        draught_margin_m=request.draught_margin_m,
    )
    opinions.append(tools.berth_opinion(berth_check))
    candidate = berth_check.candidate
    if candidate is None:
        # 배정된 시설이 **실제로** 안 맞는 경우에만 대체안을 찾는다(회의 §3 조치안).
        # 근거 부족(표기 미해소·조위 예보 없음)이면 찾지 않는다 — 어디가
        # 문제인지도 모르는 채로 다른 자리를 권하는 건 근거 없는 조언이다.
        suggestion = tools.Alternatives() if berth_check.evidence_missing else await _alternatives()
        return OrchestratorResult(
            overall_decision=OverallDecision.NO_ELIGIBLE_BERTH,
            weather_assessment=weather_result,
            evidence_missing=berth_check.evidence_missing,
            assignment_trace=[berth_check.reason] if berth_check.reason else [],
            suggested_alternatives=suggestion.candidates,
            suggestion_note=suggestion.note,
            summary=_no_candidate_summary(berth_check.reason or "사전배정 선석을 확인할 수 없습니다."),
            opinions=opinions,
        )

    # [2026-09-21, D2 S2 — 점유는 등급이 아니라 정보] 점유를 이유로 다른 자리를 찾지
    # 않는다. 배는 이미 그 부두에 붙어 있고, 질문은 "이 자리를 줄 수 있나"가 아니라
    # "이 자리가 맞나"다. PORT-MIS 출항 시각이 예정값이라 동시 계류를 과대 계산하고
    # (백테스트 7/125), 점유의 근거인 AIS 실측은 대개 검증 대상 자신이다.
    # 점유 사실은 trace 에 남겨 관제사가 "이 부두에 지금 몇 척이 있다"를 볼 수 있게 한다.
    occ_note = (
        f"참고: 이 시설에 다른 선박이 접안 중입니다(AIS 실측). "
        f"판정 등급에는 반영하지 않습니다"
        if candidate.occupancy_status is OccupancyStatus.OCCUPIED
        else None
    )
    trace = [
        f"배정된 시설 '{candidate.wharf_name}' 확인 "
        f"(수심 {candidate.depth_m}m, 흘수 {request.vessel.draught_m}m, "
        f"여유 {candidate.draught_margin_m:.1f}m)",
        *([occ_note] if occ_note else []),
    ]

    # ── 2. 기상 ────────────────────────────────────────────────────────────
    # 선석 고유 임계값(berth_group)으로 판정한다. berth_group 이 없어도 wharf_name 을
    # 넘겨 외해 부이 파고가 항내 부두에 대입되지 않게 한다(2026-09-21, D2 ②).
    berth_weather = await tools.check_weather(
        db,
        window_end=request.window_end,
        wharf_name=candidate.wharf_name,
        berth_group=candidate.berth_group,
        as_of=request.weather_as_of,
    )
    opinions.append(tools.weather_opinion(berth_weather))
    if berth_weather.status is not WorkStatus.NORMAL:
        # [2026-09-26] 선석이 아니라 기상 때문이다 — '배정된 선석이 맞지 않는다'가 아니라
        # 기상 사유로 판정한다. 관측이 없거나 낡았으면 근거 부족(판정불가)이고 대체안도
        # 찾지 않는다.
        if berth_weather.status is WorkStatus.UNKNOWN:
            return OrchestratorResult(
                overall_decision=OverallDecision.WEATHER_BLOCKED,
                weather_assessment=berth_weather,
                evidence_missing=True,
                assignment_trace=trace,
                summary=_no_candidate_summary(
                    f"'{candidate.wharf_name}' 기상 관측이 없거나 오래돼 판단할 수 없습니다."
                ),
                opinions=opinions,
            )
        suggestion = await _alternatives()
        return OrchestratorResult(
            overall_decision=OverallDecision.WEATHER_BLOCKED,
            weather_assessment=berth_weather,
            assignment_trace=trace,
            suggested_alternatives=suggestion.candidates,
            suggestion_note=suggestion.note,
            summary=(
                f"'{candidate.wharf_name}' 기상이 '{berth_weather.status.value}' 기준을 "
                f"넘었습니다: {'; '.join(berth_weather.reasons)}"
            ),
            opinions=opinions,
        )

    # ── 3. 혼재 ────────────────────────────────────────────────────────────
    safety_result = await tools.check_segregation(
        db,
        neo4j_driver,
        llm_client,
        cargo=request.cargo,
        cargos=request.cargos,
        adjacent_cargos=candidate.adjacent_cargos,
    )
    opinions.append(tools.segregation_opinion(safety_result))

    if safety_result.risk_level != RiskLevel.BLOCKED:
        # ── 4. 교차 확인 → 5. 설명 ─────────────────────────────────────────
        conditions, condition_key = _cross_check(berth_weather)
        llm_result = await _llm_summary(
            llm_client,
            selected_berth=candidate,
            safety=safety_result,
            weather=berth_weather,
            rejected=[],
            vessel=request.vessel,
            assignment_trace=trace,
        )
        return OrchestratorResult(
            overall_decision=OverallDecision.APPROVED,
            selected_berth=candidate,
            safety_assessment=safety_result,
            weather_assessment=berth_weather,
            assignment_trace=trace,
            summary=llm_result.summary,
            berth_match_summary=llm_result.berth_match_summary,
            opinions=opinions,
            conditions=conditions,
            condition_key=condition_key,
        )

    # 혼재 배정불가 — 이 선석이 이 화물에 맞지 않는다. 대체 선석을 제안한다.
    rejected = [
        RejectedCandidate(berth_id=candidate.berth_id, rank=candidate.rank, reason=safety_result.reasoning)
    ]
    llm_result = await _llm_summary(
        llm_client,
        selected_berth=None,
        safety=None,
        weather=weather_result,
        rejected=rejected,
        vessel=request.vessel,
        # weather_result 는 전역 폴백값이다 — LLM 에게 이 값이 이 선석 판정이 아님을 알린다.
        is_global_fallback_weather=True,
    )
    suggestion = await _alternatives()
    return OrchestratorResult(
        overall_decision=OverallDecision.ALL_CANDIDATES_UNSAFE,
        weather_assessment=weather_result,
        rejected_candidates=rejected,
        suggested_alternatives=suggestion.candidates,
        suggestion_note=suggestion.note,
        summary=llm_result.summary,
        # berth_match_summary는 안 담는다 — 선택된 선석이 없어 "매칭"을 말할 대상 자체가 없다.
        opinions=opinions,
    )
