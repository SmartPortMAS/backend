"""안전관제 에이전트 오케스트레이션 — **인접 선석** 간 혼재 위험 판정.

흐름: 대상 화물 조회 -> 인접 화물 조회 -> Neo4j 근거축 병행 탐색 -> 규칙엔진
하한(floor) 중 가장 심각한 쪽 채택 -> MSDS 요약 -> LLM 종합 판단 -> floor로
하한 보정 -> 최종 결과 조립.

판정에 쓰는 축(2026-08-23 기준):
    MSDS 텍스트 혼재금지 · 벌크 액체화학물질 호환성그룹 · 용기등급 대비 하역방식

판정에 쓰지 않는 축:
    IMDG Code Ch.7.2 격리표. 단일 선박 내 적부 규정이라 부두 간에는 적용
    대상이 없다(IMO도 항만 구역은 MSC.1/Circ.1216으로 분리). 조회 결과는
    응답(imdg_conflicts·imdg_unconfirmed_pairs·imdg_classes)에 참고 정보로
    싣되 등급 근거로도 LLM 프롬프트로도 쓰지 않는다. IMDG를 위험 판정에 쓰는
    곳은 동일 공간 취급을 보는 berth_alerts다.
"""

import asyncio
from dataclasses import dataclass

from neo4j import AsyncDriver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMClient
from app.models import MsdsChemical

from .bulk_compatibility import build_bulk_conflicts
from .graph_queries import (
    find_assessability_facts,
    find_bulk_exceptions,
    find_bulk_group_conflicts,
    find_bulk_groups,
    find_imdg_classes,
    find_imdg_no_segregation_required,
    find_imdg_segregation_conflicts,
    find_incompatible_conflicts,
    find_live_categories,
)
from .msds_context import resolve_cargo, summarize_hazard_sections
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .stabilized_cargo import with_stabilized_checklist
from .rule_engine import (
    Assessability,
    compute_assessability_floor,
    compute_bulk_compatibility_floor,
    compute_imdg_berth_adjacency_floor,
    compute_packing_floor,
    compute_pair_assessability,
    compute_risk_floor,
    find_packing_violation,
)
from .schemas import (
    AdjacentCargo,
    BulkCompatibilityConflict,
    ImdgSegregationConflict,
    ImdgUnconfirmedPair,
    IncompatibleConflict,
    LLMAssessment,
    PackagingViolation,
    RiskLevel,
    SafetyAssessmentRequest,
    SafetyAssessmentResult,
    SafetyVerdict,
    UnassessedPair,
    max_risk_level,
)


def _cargo_display_name(row: MsdsChemical) -> str:
    return row.name_ko or row.name_en or row.chem_id


def _assessability_reason(
    *,
    target_name: str,
    adjacent_name: str,
    target_avoids: set[str],
    target_classes: set[str],
    adjacent_avoids: set[str],
    adjacent_classes: set[str],
) -> str:
    """어느 근거가 없어 판정하지 못했는지 관제사가 읽을 문장으로 만든다.

    "판정 불가"만 알려주면 관제사는 무엇을 보완해야 할지 알 수 없다. 2-hop
    경로의 어느 끝이 비었는지까지 적어야 후속 조치(MSDS 재확인·전문가 문의)로
    이어진다.
    """
    missing: list[str] = []
    if not target_avoids:
        missing.append(f"'{target_name}'의 MSDS에 화물 대상 기피 정보 없음")
    if not adjacent_classes:
        missing.append(f"'{adjacent_name}'이 어느 혼재금지 부류에도 분류되지 않음")
    if not adjacent_avoids:
        missing.append(f"'{adjacent_name}'의 MSDS에 화물 대상 기피 정보 없음")
    if not target_classes:
        missing.append(f"'{target_name}'이 어느 혼재금지 부류에도 분류되지 않음")
    return " / ".join(missing) if missing else "판정 근거 일부 결측"


async def _resolve_adjacent_cargos(
    db: AsyncSession, adjacent_cargos: list[AdjacentCargo]
) -> list[tuple[str, float | None, MsdsChemical]]:
    """인접 화물을 (선석명, 거리, MsdsChemical)로 해석한다.

    chem_id로 지정된 화물은 단일 IN 조회로 한 번에 가져오고, cas_no로만 들어온
    화물은 resolve_cargo를 개별 호출한다 — 그쪽은 DB에 없으면 KOSHA API로
    lazy-fetch 하는 경로라 배치로 묶을 수 없다(그리고 그 경우는 드물다).
    입력 순서를 그대로 보존한다 — 호출부가 인덱스로 짝지어 쓰지는 않지만,
    응답에 나가는 인접 화물 나열 순서가 요청과 어긋나면 화면에서 읽기 어렵다.
    """
    by_chem_id: dict[str, MsdsChemical] = {}
    chem_ids = [a.cargo.chem_id for a in adjacent_cargos if a.cargo.chem_id and not a.cargo.cas_no]
    if chem_ids:
        rows = await db.scalars(select(MsdsChemical).where(MsdsChemical.chem_id.in_(chem_ids)))
        by_chem_id = {row.chem_id: row for row in rows}

    resolved: list[tuple[str, float | None, MsdsChemical]] = []
    for adjacent in adjacent_cargos:
        cached = by_chem_id.get(adjacent.cargo.chem_id) if not adjacent.cargo.cas_no else None
        row = cached if cached is not None else await resolve_cargo(db, adjacent.cargo)
        resolved.append((adjacent.berth_name, adjacent.distance_m, row))
    return resolved


@dataclass
class _Verdict:
    """LLM 없이 계산되는 판정 결과 전부.

    [2026-08-23] assess_safety를 둘로 나누며 도입했다. 등급(rule_engine_floor)과
    충돌 근거는 그래프·DB 조회만으로 확정되는데(실측 45ms), 이전에는 LLM 서술이
    끝날 때까지(약 2.4~5초) 아무것도 응답하지 못했다. 관제사가 "이 배치가
    위험한가"를 알기 위해 문장 생성이 끝나기를 기다린 셈이다.

    이 구조 덕분에 /safety/verdict는 이걸 그대로 반환하고, /safety/assess는
    여기에 LLM 서술만 얹는다 — 판정 로직이 한 곳에만 있으므로 두 엔드포인트가
    같은 질문에 다른 등급을 낼 수 없다.
    """

    target_row: MsdsChemical
    conflicts: list[IncompatibleConflict]
    imdg_conflicts: list[ImdgSegregationConflict]
    imdg_unconfirmed_pairs: list[ImdgUnconfirmedPair]
    bulk_compatibility_conflicts: list[BulkCompatibilityConflict]
    packaging_violations: list[PackagingViolation]
    unassessed_pairs: list[UnassessedPair]
    rule_engine_floor: RiskLevel
    imdg_classes: dict[str, str]
    hazard_summary: dict[str, list[str]]


async def _compute_verdict(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    request: SafetyAssessmentRequest,
) -> _Verdict:
    """LLM을 부르지 않고 등급과 근거를 확정한다."""
    target_row = await resolve_cargo(db, request.target_cargo)

    # [2026-08-23] 인접 화물 해석을 배치로 바꿨다. 이전에는 리스트 컴프리헨션
    # 안에서 화물마다 순차 await 했는데, 실제 스케줄링 응답의 인접 화물이 9종까지
    # 나와(대한유화부두 후보) 왕복이 그대로 쌓였다 — 실측 34.2ms -> 12.3ms(-64%).
    # chem_id만 있는 화물은 한 번의 IN 조회로 끝내고, cas_no로 들어온 화물만
    # 기존 lazy-fetch 경로(resolve_cargo -> 없으면 KOSHA API 호출)를 태운다.
    adjacent_resolved = await _resolve_adjacent_cargos(db, request.adjacent_cargos)
    adjacent_chem_ids = list({row.chem_id for _, _, row in adjacent_resolved})

    # [2026-08-23] 그래프 조회를 병렬로 묶었다. 일곱 개 Cypher가 서로의 결과를
    # 보지 않는 독립 조회인데 순차 await 하고 있었다 — 실측 71.4ms -> 34.1ms(-52%).
    # Neo4j 드라이버는 조회마다 자체 세션을 열므로 동시 사용에 문제가 없다
    # (PostgreSQL 세션과 달리 공유 상태가 없다).
    (
        raw_conflicts,
        raw_imdg_conflicts,
        imdg_classes,
        confirmed_no_segregation_ids,
        group_conflict_rows,
        groups_by_chem_id,
        bulk_exception_ids,
        live_categories,
        facts,
    ) = await asyncio.gather(
        find_incompatible_conflicts(
            neo4j_driver, target_chem_id=target_row.chem_id, adjacent_chem_ids=adjacent_chem_ids),
        find_imdg_segregation_conflicts(
            neo4j_driver, target_chem_id=target_row.chem_id, adjacent_chem_ids=adjacent_chem_ids),
        find_imdg_classes(
            neo4j_driver, chem_ids=[target_row.chem_id, *adjacent_chem_ids]),
        find_imdg_no_segregation_required(
            neo4j_driver, target_chem_id=target_row.chem_id, adjacent_chem_ids=adjacent_chem_ids),
        find_bulk_group_conflicts(
            neo4j_driver, target_chem_id=target_row.chem_id, adjacent_chem_ids=adjacent_chem_ids),
        find_bulk_groups(
            neo4j_driver, chem_ids=[target_row.chem_id, *adjacent_chem_ids]),
        find_bulk_exceptions(
            neo4j_driver, target_chem_id=target_row.chem_id, adjacent_chem_ids=adjacent_chem_ids),
        find_live_categories(neo4j_driver),
        find_assessability_facts(
            neo4j_driver, chem_ids=[target_row.chem_id, *adjacent_chem_ids]),
    )
    bulk_safe_exception_ids, bulk_blocked_exception_ids = bulk_exception_ids
    # 충돌 여부와 무관하게 대상·인접 화물 각자의 Class 자체를 별도로 조회한다 —
    # find_imdg_segregation_conflicts는 SEGREGATE 관계(=충돌)가 있을 때만 Class 값을
    # 같이 주므로, 통과한 화물쌍은 이 조회 없이는 Class조차 알 수 없었다(service.py
    # 상단 docstring에 이유 설명 없음 — graph_queries.find_imdg_classes 참고).
    # chem_id -> 최단거리. [2026-08-23] 이 값은 더 이상 판정에 쓰이지 않는다 —
    # 응답(ImdgSegregationConflict.distance_m)에 실어 관제사가 직접 보고 판단할
    # 수 있게 하는 표시용이다. 같은 화학물질이 여러 인접 선석에 걸쳐 있으면
    # 가장 가까운(가장 보수적인) 거리를 쓴다.
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

    # 대상·인접 화물의 IMDG Class가 둘 다 확인됐는데 SEGREGATE 관계(공인 격리규정)가
    # 없는 화물쌍 — 코드 X(개별 확인 필요)와 로더 미등재를 구분할 수 없으므로
    # "SEGREGATE 없음 = 안전"으로 단정하지 않는다 (rule_engine.compute_imdg_unconfirmed_floor 참고).
    # 단, NO_SEGREGATION_REQUIRED로 확정된 조합(대표적으로 같은 Class끼리 — 공인
    # 표상 예외 없이 X)은 여기서 제외한다. 그게 없으면 벤젠-가솔린처럼 둘 다
    # Class 3인 흔한 조합마다 근거 없는 주의 경고가 스팸처럼 났다(2026-08-21 발견).
    confirmed_imdg_chem_ids = {raw["chem_id"] for raw in raw_imdg_conflicts}
    target_imdg_class = imdg_classes.get(target_row.chem_id)
    imdg_unconfirmed_pairs: list[ImdgUnconfirmedPair] = [
        ImdgUnconfirmedPair(
            adjacent_berth=berth_name,
            adjacent_chem_id=row.chem_id,
            adjacent_name=row.name_ko or row.chem_id,
            target_imdg_class=target_imdg_class,
            adjacent_imdg_class=imdg_classes[row.chem_id],
        )
        for berth_name, _distance_m, row in adjacent_resolved
        if target_imdg_class is not None
        and row.chem_id in imdg_classes
        and row.chem_id not in confirmed_imdg_chem_ids
        and row.chem_id not in confirmed_no_segregation_ids
    ]

    # 벌크 액체화학물질 호환성 그룹(참고자료) 축 — bulk_compatibility.py 참고.
    # MSDS 텍스트 마이닝·IMDG 공인 격리표와 근거가 다른 제3의 신호로, 둘을
    # 대체하지 않고 병렬로 추가한다. 2026-08-21부터 Neo4j 그래프 조회로 판정한다
    # (챗봇도 같은 그래프를 봐야 해서 판정 로직의 권위를 그래프 하나로 통일했다).
    adjacent_names = {row.chem_id: (row.name_ko or row.chem_id) for _, _, row in adjacent_resolved}

    raw_bulk_conflicts = build_bulk_conflicts(
        target_chem_id=target_row.chem_id,
        adjacent_chem_ids=adjacent_chem_ids,
        adjacent_names=adjacent_names,
        group_conflict_ids={row["chem_id"] for row in group_conflict_rows},
        groups_by_chem_id=groups_by_chem_id,
        safe_exception_ids=bulk_safe_exception_ids,
        blocked_exception_ids=bulk_blocked_exception_ids,
    )
    bulk_compatibility_conflicts: list[BulkCompatibilityConflict] = [
        BulkCompatibilityConflict(
            adjacent_berth=berth_name,
            adjacent_chem_id=raw["chem_id"],
            adjacent_name=raw["name_ko"] or row.name_ko or row.chem_id,
            target_group=raw["group_a"][0] if raw["group_a"] else 0,
            target_group_name=raw["group_a"][2] if raw["group_a"] else "미상",
            adjacent_group=raw["group_b"][0] if raw["group_b"] else 0,
            adjacent_group_name=raw["group_b"][2] if raw["group_b"] else "미상",
            reason=raw["reason"],
        )
        for raw in raw_bulk_conflicts
        for berth_name, _distance_m, row in adjacent_resolved
        if row.chem_id == raw["chem_id"]
    ]

    # [2026-08-23] IMDG 두 축(확정 격리충돌·규정 미확인)은 이 판정에서 **제외**한다.
    #
    # 이 함수가 답하는 질문은 "인접 선석에 저 화물을 실은 배가 있어도 되는가"인데,
    # IMDG Code Ch.7.2는 단일 선박 내 적부 규정이라 부두와 부두 사이에는 적용
    # 대상이 없다(원 규정 이격거리는 3~24m이고, IMO는 항만 구역을 별도 문서
    # MSC.1/Circ.1216으로 분리해 두었다). 전수 실측에서도 이 축이 1,260개
    # 화물쌍 중 304건의 등급을 단독으로 끌어올리고 있었고, 그 실질 내용은
    # "인화성 액체 옆에 인화성 가스가 있으면 위험"이라는 한 줄이라 석유화학
    # 항만의 정상 운영 배치가 상시 경고로 뜨고 있었다.
    #
    # 조회 자체는 계속 한다 — imdg_conflicts·imdg_unconfirmed_pairs·imdg_classes는
    # 응답에 그대로 실려 관제사에게 참고 정보로 표시된다. 판정 근거에서만 뺀다.
    # compute_imdg_berth_adjacency_floor는 항상 SAFE를 돌려주지만, "의도적으로
    # 배제했다"는 사실이 코드에 남도록 호출은 유지한다(rule_engine.py 주석 참고).
    #
    # IMDG를 위험 판정에 쓰는 맥락은 동일 공간 취급(berth_alerts)이며, 그쪽은
    # compute_imdg_costowage_floor가 격리코드 강도를 반영해 판정한다.
    # 판정 가능성 축 (2026-08-23 추가) — "충돌 없음"이 "안전 확인"인지 "볼 근거가
    # 없었음"인지 가른다. 후자는 최소 주의로 격상해 관제사에게 드러낸다.
    # 근거와 실측치는 rule_engine.compute_pair_assessability 주석 참고.
    target_avoids, target_classes = facts.get(target_row.chem_id, (set(), set()))

    unassessed_pairs: list[UnassessedPair] = []
    worst_assessability = Assessability.FULL
    for berth_name, _distance_m, row in adjacent_resolved:
        adj_avoids, adj_classes = facts.get(row.chem_id, (set(), set()))
        assessability = compute_pair_assessability(
            target_avoids=target_avoids,
            target_classes=target_classes,
            adjacent_avoids=adj_avoids,
            adjacent_classes=adj_classes,
            live_categories=live_categories,
        )
        if assessability is Assessability.FULL:
            continue
        if worst_assessability is Assessability.FULL or assessability is Assessability.NONE:
            worst_assessability = assessability
        unassessed_pairs.append(
            UnassessedPair(
                adjacent_berth=berth_name,
                adjacent_chem_id=row.chem_id,
                adjacent_name=row.name_ko or row.chem_id,
                assessability=assessability.value,
                reason=_assessability_reason(
                    target_name=_cargo_display_name(target_row),
                    adjacent_name=row.name_ko or row.chem_id,
                    target_avoids=target_avoids & live_categories,
                    target_classes=target_classes,
                    adjacent_avoids=adj_avoids & live_categories,
                    adjacent_classes=adj_classes,
                ),
            )
        )

    rule_engine_floor = max_risk_level(
        max_risk_level(
            max_risk_level(
                compute_risk_floor(raw_conflicts),
                compute_imdg_berth_adjacency_floor(raw_imdg_conflicts),
            ),
            compute_packing_floor(packing_violation_raw),
        ),
        max_risk_level(
            compute_bulk_compatibility_floor(raw_bulk_conflicts),
            compute_assessability_floor(worst_assessability),
        ),
    )
    hazard_summary = summarize_hazard_sections(target_row.msds_payload)

    return _Verdict(
        target_row=target_row,
        conflicts=conflicts,
        imdg_conflicts=imdg_conflicts,
        imdg_unconfirmed_pairs=imdg_unconfirmed_pairs,
        bulk_compatibility_conflicts=bulk_compatibility_conflicts,
        packaging_violations=packaging_violations,
        unassessed_pairs=unassessed_pairs,
        rule_engine_floor=rule_engine_floor,
        imdg_classes=imdg_classes,
        hazard_summary=hazard_summary,
    )


async def assess_verdict(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    request: SafetyAssessmentRequest,
) -> SafetyVerdict:
    """등급과 근거만 확정한다 — LLM을 부르지 않는다 (실측 약 45ms).

    화면이 결론을 먼저 띄우고 서술(checklist·reasoning)은 이어서 채우는 2단계
    렌더링을 위해 만들었다. 등급은 규칙엔진이 결정하므로 이 값은 나중에
    /safety/assess가 돌려주는 risk_level과 **항상 같다** — 실측으로도 격상률이
    0%다(48회). 먼저 보여준 등급이 뒤집히지 않으므로 조기 표시가 안전하다.
    """
    v = await _compute_verdict(db, neo4j_driver, request)
    return SafetyVerdict(
        target_cargo_name=_cargo_display_name(v.target_row),
        risk_level=v.rule_engine_floor,
        rule_engine_floor=v.rule_engine_floor,
        conflicts=v.conflicts,
        imdg_conflicts=v.imdg_conflicts,
        imdg_unconfirmed_pairs=v.imdg_unconfirmed_pairs,
        bulk_compatibility_conflicts=v.bulk_compatibility_conflicts,
        packaging_violations=v.packaging_violations,
        unassessed_pairs=v.unassessed_pairs,
        msds_sections_used=list(v.hazard_summary.keys()),
        imdg_classes=v.imdg_classes,
    )


async def assess_safety(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    request: SafetyAssessmentRequest,
) -> SafetyAssessmentResult:
    """판정 + LLM 서술. 응답 형태는 종전과 동일하다."""
    v = await _compute_verdict(db, neo4j_driver, request)

    llm_result = await llm_client.generate_structured(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(
            target_cargo_name=_cargo_display_name(v.target_row),
            hazard_summary=v.hazard_summary,
            conflicts=v.conflicts,
            bulk_compatibility_conflicts=v.bulk_compatibility_conflicts,
            packaging_violations=v.packaging_violations,
            unassessed_pairs=v.unassessed_pairs,
            rule_engine_floor=v.rule_engine_floor,
        ),
        schema=LLMAssessment,
    )

    # [2026-08-23] 등급은 규칙엔진이 확정한다 — LLM은 서술만 담당한다.
    # 이전: max_risk_level(llm_result.risk_level, rule_engine_floor). LLM이 하한
    # 위로 자유롭게 올릴 수 있었는데 실측(48회)에서 그 격상이 근거와 무관했다 —
    # floor가 '안전'인 24건 전부를 '위험'으로 올렸고(100%), '위험'/'배정불가'
    # 24건은 그대로 뒀다. 결과적으로 '안전'이 한 번도 출력되지 않아 4단계
    # 체계가 '위험' 하나로 붕괴해 있었다.
    #
    # 규칙엔진 값을 그대로 쓰면 안전 636 / 주의 246 / 위험 316 / 배정불가 62로
    # 네 등급이 모두 의미 있는 크기를 갖는다. **배정불가 62건은 불변이므로
    # 자동배정에서 막히는 조합은 하나도 달라지지 않는다**(오케스트레이터는
    # BLOCKED만 후보 탈락시킴) — 바뀌는 것은 화면에 표시되는 경고 등급뿐이다.
    return SafetyAssessmentResult(
        target_cargo_name=_cargo_display_name(v.target_row),
        risk_level=v.rule_engine_floor,
        # 중합성 화물(IMDG ", STABILIZED")이면 탱크 온도·억제제 확인 항목을 규칙으로
        # 맨 앞에 붙인다 — LLM 이 뽑을지에 맡기지 않는다(stabilized_cargo.py, 2026-09-17).
        checklist=with_stabilized_checklist(
            llm_result.checklist, v.target_row.un_no, v.target_row.cas_no,
        ),
        key_hazards=llm_result.key_hazards,
        reasoning=llm_result.reasoning,
        conflicts=v.conflicts,
        # IMDG 항목은 **참고 정보**로 응답에 남긴다 — 판정 근거와 LLM 프롬프트
        # 에서는 빠졌지만, 관제사가 "이 조합이 국제 규정상 선내 격리 대상인가"를
        # 알고 싶을 수 있고 화면이 그 사실을 표시한다.
        imdg_conflicts=v.imdg_conflicts,
        imdg_unconfirmed_pairs=v.imdg_unconfirmed_pairs,
        bulk_compatibility_conflicts=v.bulk_compatibility_conflicts,
        packaging_violations=v.packaging_violations,
        unassessed_pairs=v.unassessed_pairs,
        rule_engine_floor=v.rule_engine_floor,
        msds_sections_used=list(v.hazard_summary.keys()),
        imdg_classes=v.imdg_classes,
    )
