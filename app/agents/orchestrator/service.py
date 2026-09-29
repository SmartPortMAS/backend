"""오케스트레이터 — 정해진 선석 하나가 지금 이 배·화물에 맞는가(검증모드 전용).

흐름 (감독자 — 27번 설계안):
1. 선석 도구(check_berth): 판정할 선석의 가용수심·흘수 여유·이웃 화물. 맞지 않으면
   종료 — 근거 부족이면 판정불가, 실제 부적합이면 대체 선석 **제안**을 붙인다.
2. 기상 도구(check_weather): 그 선석의 임계값으로 판정. 정상이 아니면 종료
   (관측 없음 → 판정불가, 기준 초과 → 기상 사유 + 대체 제안).
3. 혼재 도구(check_segregation): 이웃 화물과의 규칙 하한. 배정불가면 부적합 + 대체 제안.
4. 교차 확인(_cross_check): 체류 중 예보 악화 등 조건을 붙인다.
5. 마무리(_finish): 판정 기록과 같은 함수로 등급·등급 이유를 정하고, 도구 의견의 '못 본 것'을
   '확인 필요'로 모은 뒤, 판정불가가 아니면 LLM 이 관제사용 문장을 쓴다(등급은 바꾸지 못한다).
도구마다 의견(Opinion: 등급 · 확인한 것 · 못 본 것)을 모아 결과에 싣는다.

[2026-09-27] 탐색모드(선석 top-3 를 새로 고르고, 점유면 대체 선석 → 정박지 대기로
재탐색)를 걷어냈다(27번 설계안 D단계, 결정 7-3). 우리는 배정하지 않는다 — 실측(9/27):
종합 판정 콘솔이 탐색모드로 43척 중 42척에 실제 접안 선석 검증과 다른 답(다른 선석
추천·'적합 선석 없음')을 보여 주고 있었다. 정박지는 위치로 관찰만 한다(/dashboard/anchorages).
"""

import asyncio
import logging
from collections.abc import Awaitable
from datetime import timedelta, timezone

from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import tools
from app.agents.safety.schemas import RiskLevel
from app.agents.scheduling.schemas import OccupancyStatus
from app.agents.weather.schemas import WeatherAssessmentResult, WorkStatus
from app.llm.base import LLMClient
from app.models.assessment_history import AssessmentLevel
from app.services.assessment import level_from_decision

from .prompt import ASSIGNMENT_WORDS, SYSTEM_PROMPT, build_user_prompt
from .schemas import LLMSummary, OrchestratorRequest, OrchestratorResult, OverallDecision, RejectedCandidate

logger = logging.getLogger(__name__)

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
        # [2026-09-29] 무엇이 기준을 넘는 예보인지 적는다 — "하역중단 예보"만으로는 바람인지 비인지 몰랐다.
        cause = f"({' · '.join(fw.earliest_deterioration_causes)}) " if fw.earliest_deterioration_causes else ""
        return (
            [f"체류 중 {at:%m/%d %H:%M} 부터 '{fw.worst_status.value}' 예보 {cause}— "
             f"그 시각 관측이 기준을 넘으면 기상 게이트가 하역을 멈춥니다"],
            f"예보:{fw.worst_status.value}",
        )
    return [], ""


def _place(request: OrchestratorRequest, wharf_name: str) -> str:
    """판정 경로 문장의 주어 — 선석이 어디서 왔는지 그대로 적는다(2026-09-29)."""
    if request.target_source == "AIS":
        return f"지금 접안한 '{wharf_name}'"
    if request.target_source == "PORT-MIS":
        # 아직 붙지 않은 배다 — '접안'이 아니라 PORT-MIS 가 사전배정한 자리다(2026-09-29).
        return f"배정된 '{wharf_name}'"
    return f"'{wharf_name}'"


def _reported_note(request: OrchestratorRequest) -> list[str]:
    """실제 접안 부두와 PORT-MIS 신고 계류시설이 다르면 한 줄 — 신고 정정이 필요하다는 신호다.

    표기만 다른 같은 부두(띄어쓰기·대소문자)는 같다고 본다. 별칭까지는 풀지 않으므로 '다르다'고
    단정하지 않고 신고값만 적는다.
    """
    reported = (request.reported_wharf_name or "").strip()
    norm = lambda s: s.replace(" ", "").upper()  # noqa: E731
    if request.target_source != "AIS" or not reported or norm(reported) == norm(request.assigned_wharf_name):
        return []
    return [f"PORT-MIS 신고 계류시설: '{reported}'"]


def _template_summary(result: OrchestratorResult) -> str:
    """LLM 없이 쓰는 종합 문장 — 판정불가이거나 LLM 을 부르지 않는 회차(판정 잡)."""
    head = result.headline.rstrip(". ")
    if result.level == AssessmentLevel.UNKNOWN.value:
        # 빠진 근거가 '확인 필요'에 이미 적혀 있으면 같은 문장을 되풀이하지 않는다 — 판정불가 건은
        # 등급 이유와 확인 필요가 같은 문장이라 화면에 세 번 떴다(실측 장생포호안).
        if result.needs_check:
            return "판정에 필요한 근거가 없어 판정하지 못했습니다. 확인 필요 항목의 빠진 근거를 채운 뒤 다시 판정해야 합니다."
        return f"{head}. 빠진 근거를 채운 뒤 다시 판정해야 합니다."
    if result.needs_check:
        return f"{head}. 확인 필요: {', '.join(m.split(' — ')[0] for m in result.needs_check)}."
    return f"{head}."


async def _finish(
    result: OrchestratorResult, request: OrchestratorRequest, llm_client: LLMClient | None,
) -> OrchestratorResult:
    """등급·등급 이유·확인 필요를 채우고 종합 문장을 붙인다.

    등급은 판정 기록과 **같은 함수**(level_from_decision)로 정한다 — 예전엔 화면이
    overall_decision 을 그대로 써서, 기록은 '주의'인데 콘솔은 초록 '적합'으로 보였다.
    판정불가면 LLM 을 부르지 않는다 — 정리할 판정이 없고, 템플릿이 빠진 근거를 그대로 말한다.
    """
    result.target_source = request.target_source
    level, headline = level_from_decision(result)
    result.level = level.value
    result.headline = headline
    result.needs_check = list(dict.fromkeys(m for o in result.opinions for m in o.missing))
    summary = ""
    if llm_client is not None and level is not AssessmentLevel.UNKNOWN:
        llm_result = await llm_client.generate_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(result, request),
            schema=LLMSummary,
        )
        summary = llm_result.summary.strip()
        # '배정된 선석'은 PORT-MIS 사전배정 사실이라 금지어 검사에서 뺀다(등급 이유의 주어다).
        if any(w in summary.replace("배정된", "") for w in ASSIGNMENT_WORDS):
            logger.warning("종합 문장에 배정·추천 어휘 — 템플릿으로 대체: %s", summary)
            summary = ""
    result.summary = summary or _template_summary(result)
    return result


async def orchestrate(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    request: OrchestratorRequest,
    *,
    summarize: bool = True,
) -> OrchestratorResult:
    """summarize=False 면 종합 문장을 템플릿으로 쓴다(안전 서술 LLM 은 그대로 부른다).

    판정 잡이 쓴다 — 종합 문장은 판정 기록에 저장되지 않아, 잡에서 LLM 으로 쓰면 버려진다.

    [2026-09-29] 혼재 서술 LLM(체크리스트)은 판정 중에 기다리지 않고, 종합 문장 LLM 과 함께 기다린다.
    종합 문장은 규칙 결과(근거·확인 필요)만 보므로 서술을 기다릴 필요가 없다 — 두 호출이 차례로 약 6초였다.
    """
    pending: list[Awaitable[None]] = []
    result = await _judge(db, neo4j_driver, llm_client, request, defer=pending)
    result, *_ = await asyncio.gather(_finish(result, request, llm_client if summarize else None), *pending)
    return result


async def _judge(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    request: OrchestratorRequest,
    *,
    defer: list[Awaitable[None]] | None = None,
) -> OrchestratorResult:
    """판정 본체 — 종합 문장(summary)은 비워 두고 _finish 가 채운다. defer 는 assess_safety 참고."""
    # 전역 폴백 판정(__GLOBAL_DEFAULT__ 임계값). 선석을 확인하지 못했거나 혼재로 부적합인
    # 응답의 weather_assessment 를 채우는 용도다 — 통과/차단은 아래 선석별 임계값으로 한다.
    # [2026-09-29] 판정 대상 시설 이름을 넘긴다 — 파고 적용 규칙(wave_applies_to)이 이름으로 정해진다.
    #   안 넘기면 외해 파고가 대입돼, 선석 단계에서 끝난 판정의 기상 칸이 "달포부두 기상 판정: 하역중단
    #   (파고 1.6m)"으로 보였다(실측).
    weather_result = await tools.check_weather(
        db, window_end=request.window_end, wharf_name=request.assigned_wharf_name,
        as_of=request.weather_as_of,
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
        # 판정할 시설이 **실제로** 안 맞는 경우에만 대체안을 찾는다(회의 §3 조치안).
        # 근거 부족(표기 미해소·조위 예보 없음)이면 찾지 않는다 — 어디가
        # 문제인지도 모르는 채로 다른 자리를 권하는 건 근거 없는 조언이다.
        suggestion = tools.Alternatives() if berth_check.evidence_missing else await _alternatives()
        return OrchestratorResult(
            overall_decision=OverallDecision.NO_ELIGIBLE_BERTH,
            weather_assessment=weather_result,
            evidence_missing=berth_check.evidence_missing,
            assignment_trace=([berth_check.reason] if berth_check.reason else []) + _reported_note(request),
            suggested_alternatives=suggestion.candidates,
            suggestion_note=suggestion.note,
            summary="",
            opinions=opinions,
        )

    # [2026-09-21, D2 S2 — 점유는 등급이 아니라 정보] 점유를 이유로 다른 자리를 찾지
    # 않는다. 배는 이미 그 부두에 붙어 있고, 질문은 "이 자리를 줄 수 있나"가 아니라
    # "이 자리가 맞나"다. PORT-MIS 출항 시각이 예정값이라 동시 계류를 과대 계산하고
    # (백테스트 7/125), 점유의 근거인 AIS 실측은 대개 검증 대상 자신이다.
    # 점유 사실은 trace 에 남겨 관제사가 "이 부두에 지금 몇 척이 있다"를 볼 수 있게 한다.
    # 종합 문장(LLM) 입력에는 넣지 않는다 — 넣었더니 "다른 시설로 배정 고려"라고 썼다.
    occ_note = (
        f"참고: 이 시설에 다른 선박이 접안 중입니다(AIS 실측). "
        f"판정 등급에는 반영하지 않습니다"
        if candidate.occupancy_status is OccupancyStatus.OCCUPIED
        else None
    )
    trace = [
        f"{_place(request, candidate.wharf_name)} 확인 "
        f"(수심 {candidate.depth_m}m, 흘수 {request.vessel.draught_m}m, "
        f"여유 {candidate.draught_margin_m:.1f}m)",
        *_reported_note(request),
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
        # [2026-09-26] 선석이 아니라 기상 때문이다 — '선석이 맞지 않는다'가 아니라
        # 기상 사유로 판정한다. 관측이 없거나 낡았으면 근거 부족(판정불가)이고 대체안도
        # 찾지 않는다.
        if berth_weather.status is WorkStatus.UNKNOWN:
            return OrchestratorResult(
                overall_decision=OverallDecision.WEATHER_BLOCKED,
                weather_assessment=berth_weather,
                evidence_missing=True,
                assignment_trace=trace,
                summary="",
                opinions=opinions,
            )
        suggestion = await _alternatives()
        return OrchestratorResult(
            overall_decision=OverallDecision.WEATHER_BLOCKED,
            weather_assessment=berth_weather,
            assignment_trace=trace,
            suggested_alternatives=suggestion.candidates,
            suggestion_note=suggestion.note,
            summary="",
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
        call_sign=request.vessel.call_sign,
        defer=defer,
    )
    opinions.append(tools.segregation_opinion(safety_result))

    if safety_result.risk_level != RiskLevel.BLOCKED:
        # ── 4. 교차 확인 ───────────────────────────────────────────────────
        conditions, condition_key = _cross_check(berth_weather)
        return OrchestratorResult(
            overall_decision=OverallDecision.APPROVED,
            selected_berth=candidate,
            safety_assessment=safety_result,
            weather_assessment=berth_weather,
            assignment_trace=trace,
            summary="",
            opinions=opinions,
            conditions=conditions,
            condition_key=condition_key,
        )

    # 혼재 배정불가 — 이 선석이 이 화물에 맞지 않는다. 대체 선석을 제안한다.
    rejected = [
        RejectedCandidate(
            berth_id=candidate.berth_id, rank=candidate.rank,
            reason="; ".join(safety_result.verdict_basis) or safety_result.reasoning,
        )
    ]
    suggestion = await _alternatives()
    return OrchestratorResult(
        overall_decision=OverallDecision.ALL_CANDIDATES_UNSAFE,
        # [2026-09-29] 혼재 심사 결과를 싣는다 — 빠져 있어 화면이 '혼재 심사 생략'을 띄우고, 근거가
        #   선석 검증 메시지(rejected reason)에 한 덩어리로 몰렸다(사용자 지적, BUENA DIOSA).
        safety_assessment=safety_result,
        weather_assessment=weather_result,
        rejected_candidates=rejected,
        suggested_alternatives=suggestion.candidates,
        suggestion_note=suggestion.note,
        summary="",
        opinions=opinions,
    )
