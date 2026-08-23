"""MSDS 안전 챗봇 GraphRAG 파이프라인.

흐름:
  1) LLM 1회 호출로 질문 유형 분류 + 물질명 추출 (QueryPlan)
  2) 물질명 → chem_id 해석 (CAS/정확명/별칭 우선, 실패 시 pgvector)
  3) intent별 근거 수집
       incompatibility_check → 안전관제 에이전트(assess_safety) 위임 + 그래프 프로필
       incompatible_list     → 혼재금지 카테고리 및 소속 화물 조회
       chemical_info         → 그래프 프로필 + 해당 화물로 좁힌 벡터 검색
       safety_general        → 전체 코퍼스 벡터 검색
  4) LLM 2회 호출로 근거 기반 답변 생성 (LLMAnswer)
  5) 근거의 종류로 confidence 산정 (LLM이 스스로 매기게 하지 않는다)

혼재 판정을 자체 구현하지 않고 app/agents/safety에 위임하는 이유: 규칙엔진 하한,
IMDG 격리코드→위험등급 환산, LLM 하향 판정 방지 보정이 이미 거기 있고, 챗봇과
대시보드가 같은 질문에 다른 답을 내면 안 되기 때문이다.
"""

import asyncio
import logging

from neo4j import AsyncDriver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import (
    AdjacentCargo,
    CargoRef,
    SafetyAssessmentRequest,
    SafetyAssessmentResult,
    risk_level_rank,
)
from app.agents.safety.service import assess_safety
from app.core.exceptions import AppError
from app.database import AsyncSessionFactory
from app.llm.base import LLMClient
from app.llm.embeddings import EmbeddingClient
from app.models import MsdsChemical

from . import graph_queries
from .prompt import (
    ANSWER_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    build_answer_prompt,
    build_planner_prompt,
)
from .retrieval import IDENTITY_STRONG_THRESHOLD, resolve_chemical_names, search_context
from .schemas import (
    CargoHint,
    ChatResponse,
    ChemicalMatch,
    ChemicalProfile,
    Confidence,
    GraphEvidence,
    IncompatibleCategoryGroup,
    Intent,
    LLMAnswer,
    MatchMethod,
    QueryPlan,
    RagQueryRequest,
    RetrievedChunk,
)

logger = logging.getLogger(__name__)

# 혼재 판정 시 챗봇이 만들어 넣는 가상 선석 이름. 안전관제 에이전트는 인접 화물을
# "선석 이름 + 화물"로 받는데, 챗봇 질문에는 선석 개념이 없어서 필요한 자리표시자다.
_VIRTUAL_BERTH = "질문에서 지정한 인접 화물"

OUT_OF_SCOPE_ANSWER = (
    "이 챗봇은 울산항 액체화물의 MSDS 안전 정보와 혼재금지 판정만 답변할 수 있습니다. "
    "화학물질 안전과 관련된 질문을 해주세요."
)


async def answer_question(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    embedding_client: EmbeddingClient,
    request: RagQueryRequest,
) -> ChatResponse:
    # cargo_hint가 오면 LLM 1차 호출(질문 분류·물질명 추출)을 건너뛴다. 호출 측이 이미
    # 화물을 특정했으므로 추출할 게 없고, 플래너가 물질을 놓치거나 out_of_scope로
    # 오분류하는 위험도 사라진다. 다만 힌트가 없는 자유 채팅이 기본 경로다.
    hinted = await _resolve_cargo_hint(db, request.cargo_hint)
    if hinted is not None:
        plan = QueryPlan(
            intent=Intent.CHEMICAL_INFO,
            chemicals=[hinted.query_name],
            reasoning="cargo_hint로 화물이 이미 특정되어 플래너를 생략함",
        )
        resolved, unresolved, near_misses = [hinted], [], {}
        logger.info("chatbot: cargo_hint=%s 로 플래너 생략", hinted.chem_id)
    else:
        plan = await llm_client.generate_structured(
            system_prompt=PLANNER_SYSTEM_PROMPT,
            user_prompt=build_planner_prompt(request.question),
            schema=QueryPlan,
        )
        logger.info("chatbot plan: intent=%s chemicals=%s", plan.intent, plan.chemicals)

        if plan.intent is Intent.OUT_OF_SCOPE:
            return ChatResponse(
                answer=OUT_OF_SCOPE_ANSWER, intent=plan.intent, confidence=Confidence.HIGH
            )

        resolved, unresolved, near_misses = await resolve_chemical_names(
            db, embedding_client, plan.chemicals
        )

    # 물질을 지목했는데 하나도 해석되지 않았다면 그래프/MSDS를 뒤질 대상이 없다.
    # 이 경우에도 LLM을 부르는 건 환각의 지름길이라 바로 정형 응답으로 끝낸다.
    if plan.intent is not Intent.SAFETY_GENERAL and not resolved:
        return _unresolved_response(plan, unresolved, near_misses)

    intent = _adjust_intent(plan.intent, resolved)

    # [2026-08-23] "쌍 판정을 요청했는데 일부 물질이 미해석"인 상태를 표시해 둔다.
    #
    # 실측 재현: "밴젠(오타)과 가솔린을 같이 놔둘수 있어?" -> 가솔린만 해석되고
    # 밴젠은 미등재. _adjust_intent가 INCOMPATIBLE_LIST로 강등해 안전 에이전트를
    # 부르지도 않았는데(assessment=None), LLM은 "밴젠과 가솔린을 같이 두는 것은
    # 권장되지 않습니다"라고 **판정한 것처럼** 답했다. 가솔린의 혼재금지
    # 카테고리(가연성물질)를 보고 "밴젠=벤젠이니 인화성이겠지"라고 자기 지식으로
    # 메꾼 것이다.
    #
    # 프롬프트에는 이미 "일반 화학 지식으로 보충하지 마세요"(1번 규칙)와
    # [해석 실패한 물질명] 블록이 둘 다 있었지만 지켜지지 않았다. 그래서 결론
    # 문장 자체를 코드가 확정해 앞에 붙인다 — 프롬프트로 부탁하지 않고 못 박는다.
    incomplete_pairwise = plan.intent is Intent.INCOMPATIBILITY_CHECK and bool(unresolved)

    profiles = await _build_profiles(db, neo4j_driver, resolved)

    assessment: SafetyAssessmentResult | None = None
    groups: list[IncompatibleCategoryGroup] = []

    if intent is Intent.INCOMPATIBILITY_CHECK:
        assessment = await _delegate_to_safety_agent(db, neo4j_driver, llm_client, resolved)
    elif intent is Intent.INCOMPATIBLE_LIST:
        groups = await build_incompatible_groups(neo4j_driver, resolved[0].chem_id)

    chunks = await _retrieve_context(db, embedding_client, request.question, intent, resolved)
    graph_evidence = GraphEvidence(profiles=profiles, incompatible_groups=groups)

    llm_answer = await llm_client.generate_structured(
        system_prompt=ANSWER_SYSTEM_PROMPT,
        user_prompt=build_answer_prompt(
            question=request.question,
            graph_evidence=graph_evidence,
            assessment=assessment,
            chunks=chunks,
            unresolved=unresolved,
            incomplete_pairwise=incomplete_pairwise,
        ),
        schema=LLMAnswer,
    )

    return ChatResponse(
        answer=_prepend_unjudged_notice(
            llm_answer.answer, unresolved, incomplete_pairwise, near_misses
        ),
        intent=intent,
        confidence=_compute_confidence(
            resolved=resolved,
            profiles=profiles,
            assessment=assessment,
            groups=groups,
            chunks=chunks,
            data_insufficient=llm_answer.data_insufficient,
        ),
        chemicals_resolved=resolved,
        chemicals_unresolved=unresolved,
        safety_actions=llm_answer.safety_actions,
        graph_evidence=graph_evidence,
        safety_assessment=assessment,
        retrieved_chunks=chunks,
        sources=_build_sources(resolved, chunks),
    )


def _adjust_intent(intent: Intent, resolved: list[ChemicalMatch]) -> Intent:
    """해석된 물질 수와 맞지 않는 intent를 실행 가능한 쪽으로 되돌린다.

    LLM 분류는 틀릴 수 있고, 물질 하나만 해석된 상태로 혼재 판정을 돌리면
    안전관제 에이전트가 인접 화물 없는 요청을 받아 무의미한 "안전" 판정을 낸다.
    """
    if intent is Intent.INCOMPATIBILITY_CHECK and len(resolved) < 2:
        return Intent.INCOMPATIBLE_LIST
    if intent is Intent.INCOMPATIBLE_LIST and not resolved:
        return Intent.SAFETY_GENERAL
    return intent


def _suggestion_line(
    unresolved: list[str], near_misses: dict[str, tuple[str, float]]
) -> str:
    """관제사에게 되물을 "혹시 ○○을 찾으셨나요?" 한 줄. 후보가 없으면 빈 문자열.

    자동으로 바꿔치기하지 않고 되묻기만 하는 이유: 오타와 "실재하지만 이 시스템에
    없는 물질"의 유사도 구간이 완전히 겹친다(실측 0.56~0.70). 자동 교정을 켜면
    '염산'을 황산으로, '과산화수소'를 수소로 바꿔 안전 판정을 내리게 된다.
    """
    hits = [(raw, near_misses[raw][0]) for raw in unresolved if raw in near_misses]
    if not hits:
        return ""
    parts = ", ".join(f"'{raw}' → **{name}**" for raw, name in hits)
    return f"혹시 {parts}을(를) 찾으셨나요? 맞다면 그 이름으로 다시 물어봐 주세요."


def _prepend_unjudged_notice(
    answer: str,
    unresolved: list[str],
    incomplete_pairwise: bool,
    near_misses: dict[str, tuple[str, float]] | None = None,
) -> str:
    """쌍 판정을 못 한 경우, 그 사실을 코드가 확정한 문장으로 답변 맨 앞에 붙인다.

    LLM이 결론을 잘 쓰기를 기대하지 않는다 — 이 문장은 근거(unresolved 목록)에서
    기계적으로 나오므로 틀릴 수가 없고, 첫 줄에 있어 관제사가 먼저 읽는다.
    뒤따르는 LLM 문장은 등재된 화물에 대한 참고 정보로 남는다.
    """
    if not incomplete_pairwise or not unresolved:
        return answer
    names = ", ".join(f"'{n}'" for n in unresolved)
    suggestion = _suggestion_line(unresolved, near_misses or {})
    # 이 문장은 관제사가 읽는다 — "DB"·"미등재"·"레코드" 같은 시스템 용어 대신
    # 무엇이 문제이고 무엇을 확인하면 되는지를 쓴다.
    return (
        f"{names}은(는) 이 시스템이 다루는 울산항 화물 목록에 없어 "
        f"**혼재 가능 여부를 판정하지 못했습니다.**\n"
        + (f"{suggestion}\n" if suggestion else
           "화물명 표기를 다시 확인해 주세요 — 표기가 조금 다르거나 아직 등록되지 않은 "
           "화물일 수 있습니다. 화면 왼쪽 화물 목록에서 취급 가능한 화물을 보실 수 있습니다.\n")
        + "\n아래는 판정 결과가 아니라, 확인된 화물에 대한 참고 정보입니다.\n\n"
        + answer
    )


def _unresolved_response(
    plan: QueryPlan,
    unresolved: list[str],
    near_misses: dict[str, tuple[str, float]] | None = None,
) -> ChatResponse:
    names = ", ".join(unresolved) if unresolved else "질문에서 언급된 물질"
    tail = _suggestion_line(unresolved, near_misses or {}) or (
        "화물명 표기를 다시 확인해 주세요 — 화면 왼쪽 화물 목록에서 "
        "취급 가능한 화물을 보실 수 있습니다."
    )
    return ChatResponse(
        answer=(
            f"{names}은(는) 이 시스템이 다루는 울산항 화물 목록에 없어 답변드릴 수 없습니다. "
            f"{tail}"
        ),
        intent=plan.intent,
        confidence=Confidence.LOW,
        chemicals_unresolved=unresolved,
    )


# msds_chemical에서 프로필로 옮길 정형 값 컬럼 (Alembic 0007).
# 그래프 등재 여부와 무관하게 채운다 — 출처가 PostgreSQL이라 in_graph=False인
# 화물도 인화점·용기등급은 정확히 답할 수 있어야 한다.
_STRUCTURED_PROFILE_FIELDS: tuple[str, ...] = (
    "flash_point_text",
    "boiling_point_text",
    "vapor_pressure_text",
    "specific_gravity_text",
    "packing_group",
    "ems_fire",
    "ems_spill",
    "signal_word",
    "exposure_limit_kr",
)


async def _build_profiles(
    db: AsyncSession, neo4j_driver: AsyncDriver, resolved: list[ChemicalMatch]
) -> list[ChemicalProfile]:
    chem_ids = [m.chem_id for m in resolved]
    raw = await graph_queries.fetch_profiles(neo4j_driver, chem_ids)

    rows = await db.scalars(select(MsdsChemical).where(MsdsChemical.chem_id.in_(chem_ids)))
    structured = {
        row.chem_id: {f: getattr(row, f) for f in _STRUCTURED_PROFILE_FIELDS}
        for row in rows
    }

    profiles: list[ChemicalProfile] = []
    for match in resolved:
        node = raw.get(match.chem_id)
        values = structured.get(match.chem_id, {})

        if node is None:
            # PostgreSQL에는 있지만 Neo4j 적재 이후 lazy-fetch된 화물. 관계 정보가
            # 없다는 사실을 프로필에 남겨 프롬프트가 "안전"으로 오독하지 않게 한다.
            logger.warning("chem_id=%s 는 Neo4j 그래프에 없습니다 (혼재 판정 불가)", match.chem_id)
            profiles.append(
                ChemicalProfile(
                    chem_id=match.chem_id,
                    name_ko=match.name_ko,
                    name_en=match.name_en,
                    cas_no=match.cas_no,
                    in_graph=False,
                    **values,
                )
            )
            continue

        profiles.append(
            ChemicalProfile(
                chem_id=node["chem_id"],
                name_ko=node["name_ko"] or match.name_ko,
                name_en=node["name_en"] or match.name_en,
                cas_no=node["cas_no"] or match.cas_no,
                un_no=node["un_no"] or None,
                in_graph=True,
                hazard_classes=sorted(node["hazard_classes"]),
                incompatible_categories=sorted(node["incompatible_categories"]),
                classified_as=sorted(node["classified_as"]),
                imdg_classes=sorted(node["imdg_classes"]),
                **values,
            )
        )
    return profiles


async def build_incompatible_groups(
    neo4j_driver: AsyncDriver, chem_id: str
) -> list[IncompatibleCategoryGroup]:
    """혼재금지 카테고리 조회 결과를 응답 스키마로 변환한다. API에서도 직접 쓴다.

    MSDS 텍스트 마이닝(INCOMPATIBLE_WITH/IS_CLASSIFIED_AS) · IMDG 공인 격리표
    · 벌크 액체화학물질 호환성그룹 참고축, 세 근거를 모두 모은다 — 하나만 조회
    하면 그 축이 놓치는 조합을 "물어봤는데 안 나왔다"가 "정말 안전하다"로
    오독할 수 있다(safety 에이전트의 pairwise 판정은 이미 세 축 모두 쓰는데
    이 목록형 조회만 뒤처져 있었다 — 2026-08-21 감사에서 발견). category
    라벨로 세 근거의 출처를 구분해 표시한다. IMDG "미확정"(모름) 신호는
    여기 넣지 않는다 — 목록형 답변에 "아마도" 항목을 섞으면 확정 사실처럼
    오독되기 쉽다(pairwise 판정에서는 주의 등급으로만 노출).
    """
    raw_groups = await graph_queries.fetch_incompatible_groups(neo4j_driver, chem_id)
    raw_groups = raw_groups + await graph_queries.fetch_imdg_segregation_groups(neo4j_driver, chem_id)
    raw_groups = raw_groups + await graph_queries.fetch_bulk_compatibility_groups(neo4j_driver, chem_id)
    return [
        IncompatibleCategoryGroup(
            category=group["category"],
            chemicals=[
                ChemicalMatch(
                    query_name=chem["name_ko"] or chem["chem_id"],
                    chem_id=chem["chem_id"],
                    name_ko=chem["name_ko"],
                    name_en=chem["name_en"],
                    cas_no=chem["cas_no"],
                    method=MatchMethod.EXACT_NAME,
                )
                for chem in group["chemicals"]
            ],
        )
        for group in raw_groups
    ]


async def _delegate_to_safety_agent(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    resolved: list[ChemicalMatch],
) -> SafetyAssessmentResult | None:
    """화물 목록을 안전관제 에이전트로 판정한다.

    2개 화물 질문은 첫 번째를 대상, 나머지 하나를 인접 화물로 두고 한 번만
    호출한다. 3개 이상이면 그것만으로는 부족하다 — "첫 번째 vs 나머지"만
    보면 첫 번째가 관여하지 않는 조합(예: 벤젠·가솔린·황산을 물었을 때
    가솔린-황산 조합)은 어떤 호출에서도 검사되지 않는다(2026-08-21 감사에서
    발견). 그래서 3개 이상이면 각 화물을 한 번씩 대상으로 돌려 모든 조합이
    최소 한 번은 검사되게 하고, 그중 가장 심각한 등급을 채택 + 발견된 충돌을
    전부 합쳐서 보여준다.

    실패해도 챗봇 전체를 실패시키지 않는다 — 그래프 프로필과 MSDS 발췌만으로도
    "판정은 못 했지만 이런 위험성이 있다"는 답변은 가능하기 때문이다.

    3개 이상일 때의 N회 판정은 **동시에** 돌린다(2026-08-22 성능 측정). 각 호출이
    서로의 결과를 보지 않는 독립 판정이고 시간의 대부분이 LLM 응답 대기라,
    순차로 돌리면 화물 수에 비례해 그대로 늘어난다(3물질 질문 실측 약 13초).
    결과 병합(_merge_safety_results)은 순서에 의존하지 않으므로 판정 결과는
    순차 실행과 동일하다.

    ★ 병렬 분기마다 **자기 세션**을 연다. AsyncSession은 동시 사용이 불가능해
    (asyncpg "another operation is in progress"), 호출부의 세션 하나를 여러
    태스크가 같이 쓰면 판정이 아니라 DB 계층에서 먼저 깨진다. 판정은 읽기
    전용이라 세션이 갈려도 결과가 달라지지 않는다.
    """
    try:
        if len(resolved) <= 2:
            return await _assess_pair(db, neo4j_driver, llm_client, target=resolved[0], others=resolved[1:])

        async def _assess_in_own_session(index: int) -> SafetyAssessmentResult:
            async with AsyncSessionFactory() as task_db:
                return await _assess_pair(
                    task_db, neo4j_driver, llm_client,
                    target=resolved[index], others=resolved[:index] + resolved[index + 1:],
                )

        results = await asyncio.gather(
            *(_assess_in_own_session(i) for i in range(len(resolved)))
        )
        return _merge_safety_results(list(results))
    except AppError:
        logger.exception("안전관제 에이전트 위임 실패 — 그래프 근거만으로 답변합니다")
        return None


async def _assess_pair(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    *,
    target: ChemicalMatch,
    others: list[ChemicalMatch],
) -> SafetyAssessmentResult:
    request = SafetyAssessmentRequest(
        target_cargo=CargoRef(chem_id=target.chem_id, name_hint=target.name_ko),
        adjacent_cargos=[
            AdjacentCargo(
                berth_name=_VIRTUAL_BERTH,
                cargo=CargoRef(chem_id=match.chem_id, name_hint=match.name_ko),
            )
            for match in others
        ],
    )
    return await assess_safety(db, neo4j_driver, llm_client, request)


def _merge_safety_results(results: list[SafetyAssessmentResult]) -> SafetyAssessmentResult:
    """여러 대상 기준으로 나온 판정을 하나로 합친다.

    서술(reasoning/checklist/key_hazards)은 가장 심각한 등급이 나온 결과의
    것을 대표로 쓰고(그 화물 조합이 가장 중요한 근거이므로), 근거 목록
    (conflicts/imdg_conflicts/bulk_compatibility_conflicts/
    imdg_unconfirmed_pairs)은 전부 합쳐서 어느 조합에서 왔든 빠짐없이 보여준다.
    """
    worst = max(results, key=lambda r: risk_level_rank(r.risk_level))
    return worst.model_copy(update={
        "conflicts": [c for r in results for c in r.conflicts],
        "imdg_conflicts": [c for r in results for c in r.imdg_conflicts],
        "imdg_unconfirmed_pairs": [c for r in results for c in r.imdg_unconfirmed_pairs],
        "bulk_compatibility_conflicts": [c for r in results for c in r.bulk_compatibility_conflicts],
    })


async def _retrieve_context(
    db: AsyncSession,
    embedding_client: EmbeddingClient,
    question: str,
    intent: Intent,
    resolved: list[ChemicalMatch],
) -> list[RetrievedChunk]:
    """intent별 기본값으로 근거 청크를 모은다.

    top_k는 서버가 정한다 — 클라이언트가 조절하면 같은 질문에 다른 답이 나온다
    (RagQueryRequest 독스트링 참고). 값을 바꿔 실험하려면 이 함수가 아니라
    tests/eval/run_eval.py처럼 search_context()를 직접 부르면 된다.
    """
    chem_ids = [m.chem_id for m in resolved] or None

    if intent is Intent.SAFETY_GENERAL:
        # 물질이 특정되지 않은 질문은 코퍼스 전체를 봐야 하고, 여러 화물의 공통
        # 문구를 모아야 하므로 top_k를 늘린다.
        return await search_context(db, embedding_client, question, top_k=8, chem_ids=None)

    if intent is Intent.INCOMPATIBILITY_CHECK:
        # 혼재 판정 자체는 safety 에이전트가 이미 MSDS 요약까지 보고 내린다.
        # 여기서는 답변에 붙일 반응성·저장 관련 문구만 조금 더 얹는다.
        return await search_context(db, embedding_client, question, top_k=4, chem_ids=chem_ids)

    return await search_context(db, embedding_client, question, top_k=6, chem_ids=chem_ids)


async def _resolve_cargo_hint(db: AsyncSession, hint: CargoHint | None) -> ChemicalMatch | None:
    """cargo_hint를 ChemicalMatch로 해석한다. 해석 실패는 조용히 None — 힌트는
    최적화일 뿐이라, 잘못된 힌트 때문에 답변 자체가 막히면 안 된다(플래너로 폴백)."""
    if hint is None:
        return None

    stmt = select(MsdsChemical).where(MsdsChemical.quality_flag == "OK")
    if hint.chem_id:
        stmt = stmt.where(MsdsChemical.chem_id == hint.chem_id)
    else:
        stmt = stmt.where(MsdsChemical.cas_no == hint.cas_no)

    row = await db.scalar(stmt)
    if row is None:
        logger.warning("cargo_hint를 해석하지 못했습니다: %s — 플래너로 폴백", hint)
        return None

    return ChemicalMatch(
        query_name=row.name_ko or row.name_en or row.chem_id,
        chem_id=row.chem_id,
        name_ko=row.name_ko,
        name_en=row.name_en,
        cas_no=row.cas_no,
        method=MatchMethod.CAS if hint.cas_no and not hint.chem_id else MatchMethod.EXACT_NAME,
    )


def _compute_confidence(
    *,
    resolved: list[ChemicalMatch],
    profiles: list[ChemicalProfile],
    assessment: SafetyAssessmentResult | None,
    groups: list[IncompatibleCategoryGroup],
    chunks: list[RetrievedChunk],
    data_insufficient: bool,
) -> Confidence:
    """근거의 종류로 신뢰도를 정한다. LLM에게 자기 확신도를 묻지 않는다 —
    환각한 답변일수록 스스로 high를 주는 경향이 있어 신호로 쓸 수 없다."""
    if data_insufficient or not (chunks or assessment or groups):
        return Confidence.LOW

    # 물질 해석이 흔들리면 그 위에 쌓인 모든 근거가 흔들린다.
    weak_match = any(
        m.method is MatchMethod.VECTOR and (m.score or 0) < IDENTITY_STRONG_THRESHOLD
        for m in resolved
    )
    if weak_match:
        return Confidence.MEDIUM

    # 그래프 미등재 화물이 섞여 있으면 관계 근거가 불완전하다.
    if any(not p.in_graph for p in profiles):
        return Confidence.MEDIUM

    if assessment is not None or groups:
        return Confidence.HIGH

    return Confidence.MEDIUM


def _build_sources(resolved: list[ChemicalMatch], chunks: list[RetrievedChunk]) -> list[str]:
    """해석된 물질 + 실제 근거로 쓰인 청크의 출처를 합친다.

    물질명을 지목하지 않은 일반 안전 질문(safety_general)은 resolved가 비어 있어도
    특정 화물의 MSDS를 근거로 답하게 되므로, 청크 쪽 출처도 반드시 표기해야 한다.
    """
    sources: list[str] = []
    seen: set[str] = set()

    def _add(chem_id: str, name: str, cas_no: str | None) -> None:
        if chem_id in seen:
            return
        seen.add(chem_id)
        sources.append(f"MSDS {name} / CAS {cas_no}" if cas_no else f"MSDS {name}")

    for match in resolved:
        _add(match.chem_id, match.name_ko or match.name_en or match.chem_id, match.cas_no)
    for chunk in chunks:
        _add(chunk.chem_id, chunk.chemical_name, chunk.cas_no)

    return sources
