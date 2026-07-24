"""안전관제 에이전트 오케스트레이션.

흐름: 대상 화물 조회 -> 인접 화물 조회 -> Neo4j 혼재금지 충돌 탐색
-> 규칙엔진 하한(floor) 계산 -> MSDS 요약 -> LLM 종합 판단
-> floor로 하한 보정 -> 최종 결과 조립.
"""

from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMClient
from app.models import MsdsChemical

from .graph_queries import find_incompatible_conflicts
from .msds_context import resolve_cargo, summarize_hazard_sections
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .rule_engine import compute_risk_floor
from .schemas import (
    IncompatibleConflict,
    LLMAssessment,
    SafetyAssessmentRequest,
    SafetyAssessmentResult,
    max_risk_level,
)


def _cargo_display_name(row: MsdsChemical) -> str:
    return row.name_ko or row.name_en or row.chem_id


async def assess_safety(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    request: SafetyAssessmentRequest,
) -> SafetyAssessmentResult:
    target_row = await resolve_cargo(db, request.target_cargo)

    adjacent_resolved: list[tuple[str, MsdsChemical]] = [
        (adjacent.berth_name, await resolve_cargo(db, adjacent.cargo))
        for adjacent in request.adjacent_cargos
    ]
    adjacent_chem_ids = list({row.chem_id for _, row in adjacent_resolved})

    raw_conflicts = await find_incompatible_conflicts(
        neo4j_driver,
        target_chem_id=target_row.chem_id,
        adjacent_chem_ids=adjacent_chem_ids,
    )

    conflicts: list[IncompatibleConflict] = [
        IncompatibleConflict(
            adjacent_berth=berth_name,
            adjacent_chem_id=raw["chem_id"],
            adjacent_name=raw["name_ko"] or row.name_ko or row.chem_id,
            shared_category=raw["category"],
            direction=raw["direction"],
        )
        for raw in raw_conflicts
        for berth_name, row in adjacent_resolved
        if row.chem_id == raw["chem_id"]
    ]

    rule_engine_floor = compute_risk_floor(raw_conflicts)
    hazard_summary = summarize_hazard_sections(target_row.msds_payload)

    user_prompt = build_user_prompt(
        target_cargo_name=_cargo_display_name(target_row),
        hazard_summary=hazard_summary,
        conflicts=conflicts,
        rule_engine_floor=rule_engine_floor,
    )

    llm_result = await llm_client.generate_structured(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=LLMAssessment,
    )

    final_risk_level = max_risk_level(llm_result.risk_level, rule_engine_floor)

    return SafetyAssessmentResult(
        target_cargo_name=_cargo_display_name(target_row),
        risk_level=final_risk_level,
        checklist=llm_result.checklist,
        key_hazards=llm_result.key_hazards,
        reasoning=llm_result.reasoning,
        conflicts=conflicts,
        rule_engine_floor=rule_engine_floor,
        msds_sections_used=list(hazard_summary.keys()),
    )
