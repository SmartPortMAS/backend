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

import logging

from neo4j import AsyncDriver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import (
    AdjacentCargo,
    CargoRef,
    SafetyAssessmentRequest,
    SafetyAssessmentResult,
)
from app.agents.safety.service import assess_safety
from app.core.exceptions import AppError
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
    ChatRequest,
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
    request: ChatRequest,
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
        resolved, unresolved = [hinted], []
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

        resolved, unresolved = await resolve_chemical_names(db, embedding_client, plan.chemicals)

    # 물질을 지목했는데 하나도 해석되지 않았다면 그래프/MSDS를 뒤질 대상이 없다.
    # 이 경우에도 LLM을 부르는 건 환각의 지름길이라 바로 정형 응답으로 끝낸다.
    if plan.intent is not Intent.SAFETY_GENERAL and not resolved:
        return _unresolved_response(plan, unresolved)

    intent = _adjust_intent(plan.intent, resolved)
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
        ),
        schema=LLMAnswer,
    )

    return ChatResponse(
        answer=llm_answer.answer,
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


def _unresolved_response(plan: QueryPlan, unresolved: list[str]) -> ChatResponse:
    names = ", ".join(unresolved) if unresolved else "질문에서 언급된 물질"
    return ChatResponse(
        answer=(
            f"{names}은(는) 현재 DB에 등록되어 있지 않아 답변할 수 없습니다. "
            "울산항 등재 화물 목록은 사이드바(또는 GET /api/v1/chatbot/chemicals)에서 확인할 수 있습니다."
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
    """혼재금지 카테고리 조회 결과를 응답 스키마로 변환한다. API에서도 직접 쓴다."""
    raw_groups = await graph_queries.fetch_incompatible_groups(neo4j_driver, chem_id)
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
    """첫 번째 물질을 대상 화물, 나머지를 인접 화물로 두고 안전관제 에이전트를 호출한다.

    실패해도 챗봇 전체를 실패시키지 않는다 — 그래프 프로필과 MSDS 발췌만으로도
    "판정은 못 했지만 이런 위험성이 있다"는 답변은 가능하기 때문이다.
    """
    request = SafetyAssessmentRequest(
        target_cargo=CargoRef(chem_id=resolved[0].chem_id, name_hint=resolved[0].name_ko),
        adjacent_cargos=[
            AdjacentCargo(
                berth_name=_VIRTUAL_BERTH,
                cargo=CargoRef(chem_id=match.chem_id, name_hint=match.name_ko),
            )
            for match in resolved[1:]
        ],
    )
    try:
        return await assess_safety(db, neo4j_driver, llm_client, request)
    except AppError:
        logger.exception("안전관제 에이전트 위임 실패 — 그래프 근거만으로 답변합니다")
        return None


async def _retrieve_context(
    db: AsyncSession,
    embedding_client: EmbeddingClient,
    question: str,
    intent: Intent,
    resolved: list[ChemicalMatch],
) -> list[RetrievedChunk]:
    """intent별 기본값으로 근거 청크를 모은다.

    top_k는 서버가 정한다 — 클라이언트가 조절하면 같은 질문에 다른 답이 나온다
    (ChatRequest 독스트링 참고). 값을 바꿔 실험하려면 이 함수가 아니라
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
