"""오케스트레이터 오케스트레이션 (교차 검증 및 종합 의사결정, 계획서 19p).

흐름:
1. request.assigned_wharf_name이 있으면(검증모드) 그 선석 하나만 확인 후보로
   만든다. 없으면(탐색모드) 스케줄링 에이전트로 후보 선석(최대 3순위)을 새로
   탐색한다. 후보가 없으면(검증모드는 선석을 못 찾았거나, 탐색모드는 적합
   선석이 없으면) 즉시 종료.
2. 후보를 순위대로 처리한다(검증모드는 후보가 하나뿐이다). 각 후보에 대해:
   a. 점유 중이면 resolve_berth_assignment로 전용 -> 대체 -> 정박지 대기 재탐색
      (온산 MVP 이식). 정박지 대기로 귀결되면 그 자리에서 종료.
   b. 확정된 선석(전용 또는 대체)의 berth_group으로 기상을 판정한다 — 선석마다
      임계값이 달라 같은 관측치에도 판정이 갈릴 수 있다(온산 MVP 이식, "같은
      기상인데 선석마다 판정이 갈림" 차별점). 이 판정이 정상이 아니면 이 후보는
      탈락하고 다음 순위로 넘어간다. 전역 기본값(__GLOBAL_DEFAULT__)은
      berth_group이 없는(그래프에 임계값 미등록) 후보에 대한 폴백으로만
      쓰인다 — 통과/차단 판정 자체는 항상 선석별 임계값 기준이다.
   c. 안전관제 에이전트에 넣어본다. 배정불가면 다음 순위로 재탐색.
   배정불가가 아닌 첫 후보를 최종 선택하고 멈춘다.
3. 모든 후보가 탈락하면 전체 실패로 종료.
4. 승인가능/전후보배정불가 케이스만 LLM으로 관제사용 종합 의견을 작성한다
   (적합선석없음/정박지대기는 이미 결론이 명확해 LLM 호출 없이 결정적 문장을
   쓴다).
"""

from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import RiskLevel, SafetyAssessmentRequest, SafetyAssessmentResult
from app.agents.safety.service import assess_safety
from app.agents.scheduling.schemas import BerthCandidate, SchedulingRequest, VesselSpec
from app.agents.scheduling.service import (
    build_candidate_for_wharf_name,
    find_berth_candidates,
    resolve_berth_assignment,
)
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


def _no_berth_summary(cargo_category: str) -> str:
    return f"화물 카테고리('{cargo_category}') 기준으로 수심·취급 조건을 만족하는 선석이 없어 추천할 수 없습니다."


def _no_candidate_summary(reason: str) -> str:
    return f"{reason} 안전하게 배정을 진행할 수 없어 관제사의 직접 확인이 필요합니다."


def _anchorage_wait_summary(candidate: BerthCandidate, trace: list[str]) -> str:
    return (
        f"전용 선석 '{candidate.wharf_name}'이 점유 중이고 대체 가능한 선석도 없어 "
        f"정박지 대기를 권고합니다. 판단 경로: {' -> '.join(trace)}"
    )


async def orchestrate(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    request: OrchestratorRequest,
) -> OrchestratorResult:
    # 전역 폴백 판정(__GLOBAL_DEFAULT__ 임계값). 하드 게이트가 아니다 — berth_group이
    # 없는 후보를 위한 폴백, 그리고 후보 없음/전체탈락 응답의 weather_assessment
    # 필드를 채우는 용도로만 쓰인다. 통과/차단 판정은 아래 루프에서 각 후보의
    # 선석별 임계값으로 한다.
    weather_result = await assess_weather(
        db,
        WeatherAssessmentRequest(
            as_of=request.weather_as_of,
            expected_completion_at=request.window_end,
        ),
    )

    # 대체(SUBSTITUTABLE_WITH) 후보의 화물 적합성 게이트용(resolve_berth_assignment
    # category 인자, 2026-08-21). 검증모드는 build_candidate_for_wharf_name이
    # 카테고리를 계산하지 않으므로(이미 실제 배정된 선석을 확인만 하는 용도라
    # 의도적으로 미필터) None으로 두고 게이트를 건너뛴다 — 탐색모드만 채운다.
    cargo_category: str | None = None

    if request.assigned_wharf_name:
        # 검증모드 — top-3 재탐색 대신 이미 정해진 선석 하나만 확인한다.
        candidate, reason = await build_candidate_for_wharf_name(
            db,
            neo4j_driver,
            wharf_name=request.assigned_wharf_name,
            vessel=request.vessel,
            window_start=request.window_start,
            window_end=request.window_end,
            draught_margin_m=request.draught_margin_m,
        )
        if candidate is None:
            return OrchestratorResult(
                overall_decision=OverallDecision.NO_ELIGIBLE_BERTH,
                weather_assessment=weather_result,
                summary=_no_candidate_summary(reason or "사전배정 선석을 확인할 수 없습니다."),
            )
        candidates_to_try = [candidate]
    else:
        # 탐색모드(하위 호환) — 카테고리 기준 top-3를 새로 탐색한다.
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
        candidates_to_try = scheduling_result.candidates[:MAX_CANDIDATES_TO_TRY]
        cargo_category = scheduling_result.cargo_category

    rejected: list[RejectedCandidate] = []
    for candidate in candidates_to_try:
        resolution = await resolve_berth_assignment(
            db,
            neo4j_driver,
            candidate=candidate,
            vessel=request.vessel,
            window_start=request.window_start,
            window_end=request.window_end,
            category=cargo_category,
        )

        if resolution.path == "정박지대기":
            return OrchestratorResult(
                overall_decision=OverallDecision.WAITING_ANCHORAGE,
                weather_assessment=weather_result,
                anchorage_assignment=resolution.anchorage,
                assignment_trace=resolution.trace,
                summary=_anchorage_wait_summary(candidate, resolution.trace),
            )

        if resolution.path == "배정불가" or resolution.berth is None:
            rejected.append(
                RejectedCandidate(berth_id=candidate.berth_id, rank=candidate.rank, reason="; ".join(resolution.trace))
            )
            continue

        resolved_berth = resolution.berth

        # 온산 MVP 이식: 확정된 선석 고유의 기상 임계값으로 판정한다("같은
        # 기상인데 선석마다 판정이 갈림" 차별점). berth_group이 없으면(그래프에
        # 임계값 미등록) 위에서 미리 구해둔 전역 폴백 판정을 그대로 쓴다.
        #
        # ★ 정상 여부 검사는 berth_group 유무와 무관하게 항상 한다 — 예전엔
        # if resolved_berth.berth_group: 블록 안에 정상 여부 검사까지 같이
        # 있어서, berth_group이 없는 후보(SK 계열 전부 등)는 전역 폴백이
        # '하역중단'이어도 검사 자체를 건너뛰고 그대로 승인됐다(실측으로 발견,
        # 2026-08-17). berth_group 없는 후보라고 기상 검사를 면제할 이유가
        # 없다 — 오히려 전용 임계값이 없어 더 보수적으로 봐야 하는 쪽이다.
        berth_weather = weather_result
        if resolved_berth.berth_group:
            berth_weather = await assess_weather(
                db,
                WeatherAssessmentRequest(
                    berth_group=resolved_berth.berth_group,
                    as_of=request.weather_as_of,
                    expected_completion_at=request.window_end,
                ),
            )
        if berth_weather.status is not WorkStatus.NORMAL:
            rejected.append(
                RejectedCandidate(
                    berth_id=resolved_berth.berth_id,
                    rank=candidate.rank,
                    reason=(
                        f"{'선석 전용' if resolved_berth.berth_group else '전역 기본'} "
                        f"기상 임계값 기준 '{berth_weather.status.value}': "
                        f"{'; '.join(berth_weather.reasons)}"
                    ),
                )
            )
            continue

        safety_result = await assess_safety(
            db,
            neo4j_driver,
            llm_client,
            SafetyAssessmentRequest(target_cargo=request.cargo, adjacent_cargos=resolved_berth.adjacent_cargos),
        )

        if safety_result.risk_level != RiskLevel.BLOCKED:
            llm_result = await _llm_summary(
                llm_client,
                selected_berth=resolved_berth,
                safety=safety_result,
                weather=berth_weather,
                rejected=rejected,
                vessel=request.vessel,
                assignment_trace=resolution.trace,
            )
            return OrchestratorResult(
                overall_decision=OverallDecision.APPROVED,
                selected_berth=resolved_berth,
                safety_assessment=safety_result,
                weather_assessment=berth_weather,
                rejected_candidates=rejected,
                assignment_trace=resolution.trace,
                # resolution.path로 판단한다("전용"이면 요청한 그 선석 그대로 확정된
                # 것이고, "대체"만 실제로 다른 선석으로 바뀐 것이다). 요청 wharf_name
                # 문자열과 직접 비교하면 안 된다 — build_candidate_for_wharf_name이
                # mart.facility_alias로 정규화하므로 같은 선석이어도 표기가 다를 수
                # 있다(예: 'SK2부두 01' 입력 -> 'SK2부두' 정규화, 같은 선석인데
                # 문자열은 다름).
                assignment_changed=(request.assigned_wharf_name is not None and resolution.path == "대체"),
                summary=llm_result.summary,
                berth_match_summary=llm_result.berth_match_summary,
            )

        rejected.append(
            RejectedCandidate(
                berth_id=resolved_berth.berth_id, rank=candidate.rank, reason=safety_result.reasoning
            )
        )

    llm_result = await _llm_summary(
        llm_client,
        selected_berth=None,
        safety=None,
        weather=weather_result,
        rejected=rejected,
        vessel=request.vessel,
        # weather_result는 상단에서 미리 구해둔 전역 폴백값이다 — 각 후보가 실제로
        # 기상 판정까지 도달했는지와 무관하게 항상 채워진다(예: 점유/DWT 사유로
        # 기상 판정 전에 배정불가 처리된 후보뿐이면, 요약문이 "현재 하역중단
        # 상태로..."라고 써서 마치 기상 때문에 실패한 것처럼 들릴 수 있다 — 실측
        # 확인, 2026-08-17). LLM에게 이 값이 개별 후보 판정이 아님을 알려준다.
        is_global_fallback_weather=True,
    )
    return OrchestratorResult(
        overall_decision=OverallDecision.ALL_CANDIDATES_UNSAFE,
        weather_assessment=weather_result,
        rejected_candidates=rejected,
        summary=llm_result.summary,
        # berth_match_summary는 안 담는다 — 선택된 선석이 없어 "매칭"을 말할 대상
        # 자체가 없다.
    )
