"""오케스트레이터 오케스트레이션 (교차 검증 및 종합 의사결정, 계획서 19p).

흐름:
1. 기상분석 에이전트 먼저 호출. 불가/판단불가면 나머지 에이전트를 호출하지 않고
   즉시 중단 권고 (LLM 미사용 — 빠른 응답이 목적).
2. 스케줄링 에이전트로 후보 선석(최대 3순위) 조회. 후보가 없으면 즉시 종료.
3. 후보를 순위대로 안전관제 에이전트에 넣어본다. 배정불가면 다음 순위로 재탐색.
   배정불가가 아닌 첫 후보를 최종 선택하고 멈춘다.
4. 모든 후보가 배정불가면 전체 실패로 종료.
5. 승인가능/전후보배정불가 케이스만 LLM으로 관제사용 종합 의견을 작성한다
   (기상불가/적합선석없음은 이미 결론이 명확해 LLM 호출 없이 결정적 문장을 쓴다).
"""

from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import RiskLevel, SafetyAssessmentRequest, SafetyAssessmentResult
from app.agents.safety.service import assess_safety
from app.agents.scheduling.schemas import BerthCandidate, SchedulingRequest
from app.agents.scheduling.service import find_berth_candidates
from app.agents.weather.schemas import WeatherAssessmentRequest, WeatherAssessmentResult, WorkStatus
from app.agents.weather.service import assess_weather
from app.llm.base import LLMClient

from .prompt import SYSTEM_PROMPT, build_user_prompt
from .schemas import LLMSummary, OrchestratorRequest, OrchestratorResult, OverallDecision, RejectedCandidate

MAX_CANDIDATES_TO_TRY = 3


async def _llm_summary(
    llm_client: LLMClient,
    *,
    selected_berth: BerthCandidate | None,
    safety: SafetyAssessmentResult | None,
    weather: WeatherAssessmentResult,
    rejected: list[RejectedCandidate],
) -> str:
    user_prompt = build_user_prompt(
        selected_berth=selected_berth, safety=safety, weather=weather, rejected_candidates=rejected
    )
    result: LLMSummary = await llm_client.generate_structured(
        system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt, schema=LLMSummary
    )
    return result.summary


def _weather_blocked_summary(weather: WeatherAssessmentResult) -> str:
    return (
        f"기상 상태가 '{weather.status.value}'로 판정되어 스케줄링·안전관제 검토 없이 "
        f"하역 중단을 권고합니다. 근거: {'; '.join(weather.reasons)}"
    )


def _no_berth_summary(cargo_category: str) -> str:
    return f"화물 카테고리('{cargo_category}') 기준으로 수심·취급 조건을 만족하는 선석이 없어 추천할 수 없습니다."


async def orchestrate(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    request: OrchestratorRequest,
) -> OrchestratorResult:
    weather_result = await assess_weather(
        db,
        WeatherAssessmentRequest(
            as_of=request.weather_as_of,
            expected_completion_at=request.window_end,
        ),
    )

    if weather_result.status in (WorkStatus.BLOCKED, WorkStatus.UNKNOWN):
        return OrchestratorResult(
            overall_decision=OverallDecision.WEATHER_BLOCKED,
            weather_assessment=weather_result,
            summary=_weather_blocked_summary(weather_result),
        )

    scheduling_result = await find_berth_candidates(
        db,
        neo4j_driver,
        SchedulingRequest(
            vessel=request.vessel,
            cargo=request.cargo,
            window_start=request.window_start,
            window_end=request.window_end,
            draught_margin_m=request.draught_margin_m,
        ),
    )

    if not scheduling_result.candidates:
        return OrchestratorResult(
            overall_decision=OverallDecision.NO_ELIGIBLE_BERTH,
            weather_assessment=weather_result,
            summary=_no_berth_summary(scheduling_result.cargo_category),
        )

    rejected: list[RejectedCandidate] = []
    for candidate in scheduling_result.candidates[:MAX_CANDIDATES_TO_TRY]:
        safety_result = await assess_safety(
            db,
            neo4j_driver,
            llm_client,
            SafetyAssessmentRequest(target_cargo=request.cargo, adjacent_cargos=candidate.adjacent_cargos),
        )

        if safety_result.risk_level != RiskLevel.BLOCKED:
            summary = await _llm_summary(
                llm_client,
                selected_berth=candidate,
                safety=safety_result,
                weather=weather_result,
                rejected=rejected,
            )
            return OrchestratorResult(
                overall_decision=OverallDecision.APPROVED,
                selected_berth=candidate,
                safety_assessment=safety_result,
                weather_assessment=weather_result,
                rejected_candidates=rejected,
                summary=summary,
            )

        rejected.append(
            RejectedCandidate(berth_id=candidate.berth_id, rank=candidate.rank, reason=safety_result.reasoning)
        )

    summary = await _llm_summary(
        llm_client, selected_berth=None, safety=None, weather=weather_result, rejected=rejected
    )
    return OrchestratorResult(
        overall_decision=OverallDecision.ALL_CANDIDATES_UNSAFE,
        weather_assessment=weather_result,
        rejected_candidates=rejected,
        summary=summary,
    )
