"""안전관제 에이전트 오케스트레이션.

흐름: 대상 화물 조회 -> 인접 화물 조회 -> Neo4j 혼재금지 충돌 탐색(MSDS 텍스트
기반 + IMDG 공인 격리표 기반, 서로 독립적으로 병행 조회) -> 두 규칙엔진 하한
(floor) 중 더 심각한 쪽 채택 -> MSDS 요약 -> LLM 종합 판단 -> floor로 하한
보정 -> 최종 결과 조립.
"""

from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMClient
from app.models import MsdsChemical

from .graph_queries import find_imdg_classes, find_imdg_segregation_conflicts, find_incompatible_conflicts
from .msds_context import resolve_cargo, summarize_hazard_sections
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .rule_engine import (
    compute_imdg_floor,
    compute_packing_floor,
    compute_risk_floor,
    find_packing_violation,
)
from .schemas import (
    ImdgSegregationConflict,
    IncompatibleConflict,
    LLMAssessment,
    PackagingViolation,
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

    adjacent_resolved: list[tuple[str, float | None, MsdsChemical]] = [
        (adjacent.berth_name, adjacent.distance_m, await resolve_cargo(db, adjacent.cargo))
        for adjacent in request.adjacent_cargos
    ]
    adjacent_chem_ids = list({row.chem_id for _, _, row in adjacent_resolved})

    raw_conflicts = await find_incompatible_conflicts(
        neo4j_driver,
        target_chem_id=target_row.chem_id,
        adjacent_chem_ids=adjacent_chem_ids,
    )
    raw_imdg_conflicts = await find_imdg_segregation_conflicts(
        neo4j_driver,
        target_chem_id=target_row.chem_id,
        adjacent_chem_ids=adjacent_chem_ids,
    )
    # 충돌 여부와 무관하게 대상·인접 화물 각자의 Class 자체를 별도로 조회한다 —
    # find_imdg_segregation_conflicts는 SEGREGATE 관계(=충돌)가 있을 때만 Class 값을
    # 같이 주므로, 통과한 화물쌍은 이 조회 없이는 Class조차 알 수 없었다(service.py
    # 상단 docstring에 이유 설명 없음 — graph_queries.find_imdg_classes 참고).
    imdg_classes = await find_imdg_classes(
        neo4j_driver,
        chem_ids=[target_row.chem_id, *adjacent_chem_ids],
    )
    # rule_engine.compute_imdg_floor가 거리 기준으로 등급을 완화할 수 있게
    # chem_id -> 최단거리를 붙여준다(2026-08-16). 같은 화학물질이 여러 인접
    # 선석에 걸쳐 있으면 가장 가까운(가장 보수적인) 거리를 쓴다.
    chem_to_min_distance: dict[str, float | None] = {}
    for _, distance_m, row in adjacent_resolved:
        current = chem_to_min_distance.get(row.chem_id, None)
        if distance_m is not None and (current is None or distance_m < current):
            chem_to_min_distance[row.chem_id] = distance_m
        elif row.chem_id not in chem_to_min_distance:
            chem_to_min_distance[row.chem_id] = None
    for raw in raw_imdg_conflicts:
        raw["distance_m"] = chem_to_min_distance.get(raw["chem_id"])

    conflicts: list[IncompatibleConflict] = [
        IncompatibleConflict(
            adjacent_berth=berth_name,
            adjacent_chem_id=raw["chem_id"],
            adjacent_name=raw["name_ko"] or row.name_ko or row.chem_id,
            shared_category=raw["category"],
            direction=raw["direction"],
        )
        for raw in raw_conflicts
        for berth_name, _distance_m, row in adjacent_resolved
        if row.chem_id == raw["chem_id"]
    ]
    imdg_conflicts: list[ImdgSegregationConflict] = [
        ImdgSegregationConflict(
            adjacent_berth=berth_name,
            adjacent_chem_id=raw["chem_id"],
            adjacent_name=raw["name_ko"] or row.name_ko or row.chem_id,
            target_imdg_class=raw["target_class"],
            adjacent_imdg_class=raw["adjacent_class"],
            segregation_code=raw["segregation_code"],
            distance_m=raw.get("distance_m"),
        )
        for raw in raw_imdg_conflicts
        for berth_name, _distance_m, row in adjacent_resolved
        if row.chem_id == raw["chem_id"]
    ]

    packing_violation_raw = find_packing_violation(
        target_row.packing_group, request.target_cargo.unload_method_name
    )
    packaging_violations = (
        [PackagingViolation(**packing_violation_raw)] if packing_violation_raw else []
    )

    rule_engine_floor = max_risk_level(
        max_risk_level(compute_risk_floor(raw_conflicts), compute_imdg_floor(raw_imdg_conflicts)),
        compute_packing_floor(packing_violation_raw),
    )
    hazard_summary = summarize_hazard_sections(target_row.msds_payload)

    user_prompt = build_user_prompt(
        target_cargo_name=_cargo_display_name(target_row),
        hazard_summary=hazard_summary,
        conflicts=conflicts,
        imdg_conflicts=imdg_conflicts,
        packaging_violations=packaging_violations,
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
        imdg_conflicts=imdg_conflicts,
        packaging_violations=packaging_violations,
        rule_engine_floor=rule_engine_floor,
        msds_sections_used=list(hazard_summary.keys()),
        imdg_classes=imdg_classes,
    )
