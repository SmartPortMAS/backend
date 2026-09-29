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
import re
from dataclasses import dataclass, field

from neo4j import AsyncDriver
from sqlalchemy import select, text
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
from .msds_context import ghs_classification, resolve_cargo, summarize_hazard_sections
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .stabilized_cargo import with_stabilized_checklist
from .rule_engine import (
    PACKING_GROUP_HIGH_RISK,
    Assessability,
    compute_assessability_floor,
    compute_bulk_compatibility_floor,
    compute_imdg_berth_adjacency_floor,
    compute_packing_floor,
    compute_pair_assessability,
    compute_risk_floor_by_adjacent,
    find_packing_violation,
)
from .schemas import (
    AdjacentCargo,
    BulkCompatibilityConflict,
    CargoRef,
    CargoVerdictSummary,
    ImdgSegregationConflict,
    ImdgUnconfirmedPair,
    IncompatibleConflict,
    LLMAssessment,
    OnboardConflict,
    PackagingViolation,
    RiskLevel,
    SafetyAssessmentRequest,
    risk_level_rank,
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


def _bulk_chart_applies(group: tuple[int, str, str] | None) -> bool:
    """46 CFR Part 150 그룹 차트로 판정할 수 있는 그룹인가 — 그룹이 있고 특수물질(0)이 아니다."""
    return group is not None and group[1] != "special"


def _bulk_group_gap(
    *,
    target_name: str,
    adjacent_name: str,
    target_group: tuple[int, str, str] | None,
    adjacent_group: tuple[int, str, str] | None,
) -> str:
    """벌크 그룹 쪽에서 무엇이 비어 판정 근거가 MSDS로 넘어왔는지 적는다."""
    missing = [
        f"'{name}'은 46 CFR Part 150 호환성 그룹 " + ("미등재" if group is None else "특수물질(차트 비적용)")
        for name, group in ((target_name, target_group), (adjacent_name, adjacent_group))
        if not _bulk_chart_applies(group)
    ]
    return " / ".join(missing)


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
    unload_method_name: str | None = None  # 포장 검사를 했는지 LLM 입력에 적는다
    adjacent_names: list[str] = field(default_factory=list)  # 비교한 이웃 화물 이름(중복 제거, 2026-09-29)


_ONBOARD_LABEL = "(같은 선박)"


async def _compute_onboard_conflicts(
    db: AsyncSession, neo4j_driver: AsyncDriver, verdicts: list["_Verdict"],
) -> list[OnboardConflict]:
    """같은 선박이 함께 실은 화물끼리의 혼재 충돌 (2026-09-28).

    인접 선석 판정과 **같은 함수**(_compute_verdict)에 나머지 화물을 이웃으로 넣어 돌리고,
    실제 충돌(MSDS 혼재금지·46 CFR 150 호환성 그룹)만 모은다. '근거 부족'(unassessed)은
    쓰지 않는다 — 한 배 안의 화물쌍은 인접 선석보다 훨씬 많아 그것까지 경고로 올리면
    다화물 선박이 거의 전부 '주의'가 되고, 실제 충돌이 묻힌다.
    화물쌍은 방향과 무관하게 한 번만 적는다(A↔B 와 B↔A 는 같은 쌍).
    """
    if len(verdicts) < 2:
        return []
    names = {v.target_row.chem_id: _cargo_display_name(v.target_row) for v in verdicts}
    seen: set[tuple[frozenset[str], str, str]] = set()
    out: list[OnboardConflict] = []

    def add(a: str, b: str, basis: str, detail: str) -> None:
        key = (frozenset((a, b)), basis, detail)
        if key in seen:
            return
        seen.add(key)
        out.append(OnboardConflict(
            cargo_a_chem_id=a, cargo_a_name=names[a], cargo_b_chem_id=b, cargo_b_name=names[b],
            basis=basis, detail=detail,
        ))

    for v in verdicts:
        a = v.target_row.chem_id
        others = [
            AdjacentCargo(berth_name=_ONBOARD_LABEL, cargo=CargoRef(chem_id=o.target_row.chem_id))
            for o in verdicts if o.target_row.chem_id != a
        ]
        ov = await _compute_verdict(
            db, neo4j_driver, SafetyAssessmentRequest(target_cargo=CargoRef(chem_id=a), adjacent_cargos=others),
        )
        for c in ov.bulk_compatibility_conflicts:
            groups = sorted([(c.target_group, c.target_group_name), (c.adjacent_group, c.adjacent_group_name)])
            add(a, c.adjacent_chem_id, "46 CFR 150", " ↔ ".join(f"{n}({g})" for g, n in groups))
        for c in ov.conflicts:
            add(a, c.adjacent_chem_id, "MSDS", f"{c.shared_category} 혼재금지")
    return out


# [2026-09-29] 하역방식은 msds 가 아니라 화물 신고(입항 건)마다 다른 값이다. 호출부가 넘기지 않아
#   신고에 '펌프'가 다 있는데도 용기등급 Ⅰ 화물마다 "하역방식 신고가 없어 보지 못했습니다"가 붙었다
#   (실측 6시간 51건 중 20건, 혼재 심사 페이지도 같음).
#   입항 건은 고정하지 않고, 이 배의 신고 중 **그 화물이 실린 가장 최근 입항 건**에서 가져온다.
#   mart.vessel_current_call 로 한정했더니 '지금+12시간 이내 입항'만 보는 뷰라, 다음 입항 예정 건의
#   화물(화면·콘솔이 판정하는 화물)과 어긋나 못 찾았다(실측 갤럭시 삼부: 판정 화물은 9/30 입항 건,
#   뷰는 9/25 건). 판정 대상 화물은 호출부가 정하므로 여기선 chem_id 로 맞추기만 한다.
_SQL_UNLOAD_METHODS = text("""
    SELECT DISTINCT ON (cm.chem_id) cm.chem_id, cm.unload_method_name
    FROM mart.cargo_msds cm
    WHERE cm.callsgn = upper(btrim(:cs)) AND cm.chem_id IS NOT NULL
      AND nullif(btrim(cm.unload_method_name), '') IS NOT NULL
    ORDER BY cm.chem_id, cm.entry_year DESC, cm.entry_count DESC, cm.bl_no
""")


async def _unload_methods(db: AsyncSession, call_sign: str) -> dict[str, str]:
    """화물(chem_id)별 신고 하역방식 — 이 배가 그 화물을 실은 가장 최근 입항 건 기준."""
    rows = (await db.execute(_SQL_UNLOAD_METHODS, {"cs": call_sign})).all()
    return {r.chem_id: r.unload_method_name.strip() for r in rows}


async def _compute_verdicts(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    request: SafetyAssessmentRequest,
) -> tuple[_Verdict, list[_Verdict], list[CargoVerdictSummary], list[OnboardConflict], RiskLevel]:
    """배가 실은 화물마다 판정하고, 가장 위험한 화물의 판정을 대표로 돌려준다.

    [2026-09-25] 전에는 target_cargo 하나만 봤다. 입항 판정이 그 배의 화물 여러 개 중
    ORDER BY 없는 LIMIT 1 로 하나를 골랐기 때문에, 메탄올이 옆 배 화물과 충돌해도
    벤젠이 뽑히면 '안전'이 나왔다. 이제 화물마다 같은 판정 함수를 돌린다(화물당
    실측 약 45ms). 등급이 같을 때 대표를 고르는 순서는 _pick_governing.

    [2026-09-28] 같은 선박 화물끼리도 비교한다(_compute_onboard_conflicts). 충돌이 있으면
    배의 등급(네 번째 반환값)을 최소 '주의'로 올린다 — 격리 적재 확인 필요이지 배정불가가
    아니다(IBC Code 3.1.3). 화물별 등급(cargo_verdicts)은 인접 선석 기준 그대로다.
    """
    verdicts: list[_Verdict] = []
    seen_chem: set[str] = set()
    methods = await _unload_methods(db, request.call_sign) if request.call_sign else {}
    for cargo in request.all_targets():
        verdict = await _compute_verdict(db, neo4j_driver, request, target_cargo=cargo, unload_methods=methods)
        # 같은 물질이 다른 키(cas_no 만 / chem_id)로 두 번 올 수 있다 — 해석된 chem_id 로 한 번만.
        if verdict.target_row.chem_id in seen_chem:
            continue
        seen_chem.add(verdict.target_row.chem_id)
        verdicts.append(verdict)
    governing = _pick_governing(verdicts)
    summaries = [
        CargoVerdictSummary(
            target_cargo_name=_cargo_display_name(v.target_row),
            chem_id=v.target_row.chem_id,
            risk_level=v.rule_engine_floor,
            conflict_count=len(v.conflicts),
            bulk_conflict_count=len(v.bulk_compatibility_conflicts),
            packaging_violation_count=len(v.packaging_violations),
            unassessed_count=len(v.unassessed_pairs),
            is_governing=v is governing,
        )
        for v in verdicts
    ]
    onboard = await _compute_onboard_conflicts(db, neo4j_driver, verdicts)
    ship_level = max_risk_level(
        governing.rule_engine_floor, RiskLevel.CAUTION if onboard else RiskLevel.SAFE,
    )
    return governing, verdicts, summaries, onboard, ship_level


# 용기등급이 높을수록(Ⅰ 이 가장 위험) 앞선다. 규칙 엔진과 같은 표기('Ⅰ')만 센다.
_PACKING_RANK = {PACKING_GROUP_HIGH_RISK: 3, "Ⅱ": 2, "Ⅲ": 1}


def _pick_governing(verdicts: list[_Verdict]) -> _Verdict:
    """대표 화물 — 등급이 가장 높은 화물. 같으면 용기등급, 그다음 인화점이 낮은 쪽 (2026-09-29).

    대표 화물의 MSDS 로 LLM 이 화물 특성·체크리스트를 쓴다. 예전엔 등급이 같으면 요청 순서상
    앞 화물을 골라, 6종이 모두 '안전'인 배에서 케로젠(Ⅲ)이 대표가 되고 가솔린(인화점 -40℃)·
    용기등급 Ⅰ 화물의 유해성은 화면 어디에도 없었다. 모두 같으면 요청 순서상 앞 화물이다.
    """
    def key(iv: tuple[int, _Verdict]):
        i, v = iv
        fp = v.target_row.flash_point_celsius
        return (
            risk_level_rank(v.rule_engine_floor),
            _PACKING_RANK.get(v.target_row.packing_group, 0),
            -fp if fp is not None else float("-inf"),
            -i,
        )
    return max(enumerate(verdicts), key=key)[1]


async def _compute_verdict(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    request: SafetyAssessmentRequest,
    *,
    target_cargo: CargoRef | None = None,
    unload_methods: dict[str, str] | None = None,
) -> _Verdict:
    """화물 하나에 대해 LLM을 부르지 않고 등급과 근거를 확정한다.

    unload_methods — 이번 입항 건 신고의 chem_id별 하역방식(_unload_methods). 요청에 하역방식이
    없는 화물만 채운다. 해석된 chem_id 로 찾으므로 CAS 로만 온 화물도 맞는다.
    """
    target_cargo = target_cargo or request.target_cargo
    target_row = await resolve_cargo(db, target_cargo)
    if not target_cargo.unload_method_name and (unload_methods or {}).get(target_row.chem_id):
        target_cargo = target_cargo.model_copy(update={"unload_method_name": unload_methods[target_row.chem_id]})

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
        target_row.packing_group, target_cargo.unload_method_name
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
    #
    # [2026-09-27] 벌크 호환성 그룹을 판정 가능성의 1차 근거로 올렸다.
    # MSDS 축은 KOSHA 10항 '피해야 할 물질'(J08)에 기댄다. 원문 직접 호출로 확인하니
    # 151종 중 91종이 '자료없음', 26종이 "가연성 물질, 환원성 물질" 같은 상투 문구라
    # MSDS만으로는 대부분의 쌍이 '볼 근거 없음'이 된다. 반면 46 CFR Part 150은
    # 두 화물의 그룹을 알면 Figure 1(그룹 차트)과 Appendix I(개별 예외)로 그 쌍의
    # 호환 여부가 규정상 정해진다 — 인접 탱크 기준이라 인접 선석보다 엄격하다.
    # 그래서 양쪽 그룹이 모두 확인되고 둘 다 특수물질(0번, 차트 비적용)이 아니면
    # '판정함'으로 본다. MSDS 충돌은 이와 별개로 계속 등급을 올린다(명시된 위험은 그대로).
    target_avoids, target_classes = facts.get(target_row.chem_id, (set(), set()))
    target_bulk_group = groups_by_chem_id.get(target_row.chem_id)

    unassessed_pairs: list[UnassessedPair] = []
    worst_assessability = Assessability.FULL
    for berth_name, _distance_m, row in adjacent_resolved:
        adj_bulk_group = groups_by_chem_id.get(row.chem_id)
        if _bulk_chart_applies(target_bulk_group) and _bulk_chart_applies(adj_bulk_group):
            continue
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
                reason=_bulk_group_gap(
                    target_name=_cargo_display_name(target_row),
                    adjacent_name=row.name_ko or row.chem_id,
                    target_group=target_bulk_group,
                    adjacent_group=adj_bulk_group,
                ) + " / " + _assessability_reason(
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
                compute_risk_floor_by_adjacent(raw_conflicts),
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
        unload_method_name=target_cargo.unload_method_name,
        adjacent_names=list(dict.fromkeys(row.name_ko or row.chem_id for _, _, row in adjacent_resolved)),
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
    v, verdicts, summaries, onboard, ship_level = await _compute_verdicts(db, neo4j_driver, request)
    return SafetyVerdict(
        target_cargo_name=_focus_name(_focus(verdicts, onboard)),
        risk_level=ship_level,
        rule_engine_floor=ship_level,
        conflicts=v.conflicts,
        imdg_conflicts=v.imdg_conflicts,
        imdg_unconfirmed_pairs=v.imdg_unconfirmed_pairs,
        bulk_compatibility_conflicts=v.bulk_compatibility_conflicts,
        packaging_violations=v.packaging_violations,
        unassessed_pairs=v.unassessed_pairs,
        msds_sections_used=list(v.hazard_summary.keys()),
        imdg_classes=v.imdg_classes,
        cargo_verdicts=summaries,
        onboard_conflicts=onboard,
        adjacent_count=len(request.adjacent_cargos),
        verdict_basis=_verdict_basis(verdicts, len(request.adjacent_cargos), onboard),
        needs_check=_needs_check(verdicts),
    )


def _adjacent_label(verdicts: list[_Verdict], adjacent_count: int) -> str:
    """'이웃 화물 5건(가솔린, 케로젠 외 1종)' — 몇 건인지만 말하지 말고 무엇인지 적는다(2026-09-29)."""
    names = getattr(verdicts[0], "adjacent_names", []) if verdicts else []
    if not names:
        return f"이웃 화물 {adjacent_count}건"
    shown = ", ".join(names[:5]) + (f" 외 {len(names) - 5}종" if len(names) > 5 else "")
    return f"이웃 화물 {adjacent_count}건({shown})"


# ── 주요 위험성 — MSDS GHS 분류에서 코드가 만든다 (2026-09-29) ──
# LLM 에 여러 화물을 한 번에 쓰게 했더니 가장 센 물질의 특성을 전부에 씌웠다(실측: 디젤·케로젠(인화성
# 구분3)을 '고인화성', 네 물질 모두 분류에 없는 '눈 자극', 3종이 해당하는 흡인 유해성 누락). 분류는
# MSDS 에 확정돼 있으니 그대로 옮긴다. 하역 현장에서 중요한 순서로 둔다.
_HAZARD_ORDER = ("인화성", "흡인 유해성", "발암성", "생식세포 변이원성", "생식독성", "흡입 독성", "급성 독성",
                 "특정표적장기 독성", "피부", "눈", "호흡기 과민성", "수생")
_FLAMMABLE_GRADE = {"구분1": "극인화성", "구분2": "고인화성", "구분3": "인화성", "구분4": "가연성"}
_SUSPECTED = ("발암성", "생식세포 변이원성", "생식독성")


def _hazard_label(name: str, grade: str) -> tuple[str, str]:
    """(묶음 제목, 그 안의 구분 표기). 괄호를 겹치지 않게 '3, 호흡기 자극'처럼 쉼표로 편다."""
    if name.startswith("인화성 액체"):
        return "인화성 액체", _FLAMMABLE_GRADE.get(grade, grade)
    # '구분' 글자는 뺀다 — 항목마다 반복돼 읽기만 무거웠다(사용자 요청 9/29). '구분1' → '1'.
    flat = grade.replace("구분", "").replace("(", ", ").replace(")", "").strip()
    if name.startswith("급성 독성(흡입"):
        route = name[name.find(":") + 1:].strip(" )") if ":" in name else ""
        return "흡입 독성", f"{flat}, {route}" if route else flat
    if name.startswith(_SUSPECTED):
        return name, f"{flat}, 의심" if flat == "2" else flat
    return name, flat


def _hazard_lines(focus: list[_Verdict]) -> list[str]:
    """설명할 화물들의 GHS 분류를 항목별로 묶는다. 화물이 하나면 이름 없이 '인화성 액체 고인화성'.

    한 항목 안에서는 센 구분이 앞에 온다(원문 '구분1' < '구분2' 순).
    """
    per_label: dict[str, dict[str, tuple[str, list[str]]]] = {}
    for x in focus:
        for name, grade in ghs_classification(x.target_row.msds_payload):
            label, shown = _hazard_label(name, grade)
            per_label.setdefault(label, {}).setdefault(shown, (grade, []))[1].append(_cargo_display_name(x.target_row))

    def rank(label: str) -> int:
        return next((i for i, k in enumerate(_HAZARD_ORDER) if k in label), len(_HAZARD_ORDER))

    lines = []
    for label in sorted(per_label, key=rank):
        grades = sorted(per_label[label].items(), key=lambda kv: kv[1][0])
        if len(focus) == 1:
            lines.append(f"{label} {' · '.join(g for g, _ in grades)}")
        else:
            lines.append(f"{label}: " + " · ".join(f"{'·'.join(names)}({g})" for g, (_, names) in grades))
    return lines


def _focus(verdicts: list[_Verdict], onboard: list[OnboardConflict]) -> list[_Verdict]:
    """설명할 화물 — 안전이 아닌 화물만, 모두 안전이면 전부 (2026-09-29).

    예전엔 대표 화물 하나로 설명해, 2종 모두 안전인 배도 "염화비닐 기준"으로만 읽혔다. 문제가
    있으면 그 화물만 말하고, 없으면 실은 화물을 묶어 말한다. 인접 기준으로는 모두 안전인데 같은
    선박 화물끼리 충돌한 배는 그 충돌에 걸린 화물이 대상이다. 위험한 화물이 앞에 온다.
    """
    risky = [x for x in verdicts if x.rule_engine_floor is not RiskLevel.SAFE]
    if not risky and onboard:
        names = {c.cargo_a_name for c in onboard} | {c.cargo_b_name for c in onboard}
        risky = [x for x in verdicts if _cargo_display_name(x.target_row) in names]
    return sorted(risky or verdicts, key=lambda x: -risk_level_rank(x.rule_engine_floor))


def _focus_name(focus: list[_Verdict]) -> str:
    return "·".join(_cargo_display_name(x.target_row) for x in focus)


# 여러 화물 MSDS 를 한 번에 넘길 때 화물·절마다 앞 몇 줄만 — 6종이면 발췌가 여섯 배가 된다.
_MULTI_HAZARD_LINES = 3


def _merged_hazards(focus: list[_Verdict]) -> dict[str, list[str]]:
    """설명할 화물들의 MSDS 발췌를 절별로 합친다. 줄마다 어느 화물인지 붙인다."""
    if len(focus) == 1:
        return focus[0].hazard_summary
    merged: dict[str, list[str]] = {}
    for x in focus:
        name = _cargo_display_name(x.target_row)
        for section, lines in x.hazard_summary.items():
            merged.setdefault(section, []).extend(f"[{name}] {line}" for line in lines[:_MULTI_HAZARD_LINES])
    return merged


def _checked_facts(
    focus: list[_Verdict], adjacent_count: int, onboard: list[OnboardConflict], ship_level: RiskLevel,
) -> str:
    """무엇을 검사했고 결과가 무엇인지 — 코드가 확정하는 첫 문장.

    [2026-09-27] LLM 에 맡기면 이웃 0건·하역방식 미신고인 판정에서도 "인접 선석 충돌
    없음으로 확인", "포장·하역방식 검증이 이루어졌으며", "기본 조치는 이행 중"처럼 하지
    않은 검사를 했다고 썼다(입력 문구를 바로잡은 뒤에도 재현). 챗봇 결론 등급과 같은
    원칙으로, 검사 사실은 근거에서 기계적으로 만든다.
    [2026-09-29] 대표 화물 하나가 아니라 _focus 의 화물들 기준이다 — 모두 안전이면 전 화물 종합.
    """
    conflicts = [c for x in focus for c in x.conflicts]
    bulk = [c for x in focus for c in x.bulk_compatibility_conflicts]
    parts: list[str] = []
    if adjacent_count == 0:
        parts.append("인접 선석에 비교할 화물이 없어 혼재 검사 대상이 없었습니다")
    else:
        n_conf = len({c.adjacent_chem_id for c in conflicts} | {c.adjacent_chem_id for c in bulk})
        if n_conf:
            parts.append(f"인접 화물 중 {n_conf}종과 충돌이 있습니다"
                         f"(MSDS {len(conflicts)}건 · 46 CFR 150 {len(bulk)}건)")
        else:
            parts.append(f"{_adjacent_label(focus, adjacent_count)}과 MSDS·46 CFR 150 기준 충돌이 없습니다")
        n_un = len({p.adjacent_chem_id for x in focus for p in x.unassessed_pairs})
        if n_un:
            parts.append(f"그중 {n_un}종은 판정 근거가 없어 확인이 필요합니다")
    # [2026-09-29] 하역방식은 용기등급 Ⅰ 화물만 판정한다(_needs_check 와 같은 기준). 그 밖의 화물에
    #   "하역방식이 신고되지 않아 검사하지 않았습니다"를 붙이면, 확인 필요에서 뺀 말이 여기서 다시 나왔다.
    if any(x.packaging_violations for x in focus):
        parts.append("포장·하역방식 부적합이 있습니다")
    elif any(x.target_row.packing_group == PACKING_GROUP_HIGH_RISK and not x.unload_method_name for x in focus):
        parts.append("용기등급 Ⅰ 화물인데 하역방식이 신고되지 않아 포장·하역방식은 검사하지 않았습니다")
    elif any(x.unload_method_name for x in focus):
        parts.append("포장·하역방식 부적합은 없습니다")
    if onboard:
        pairs = sorted({f"{c.cargo_a_name}↔{c.cargo_b_name}" for c in onboard})
        parts.append(f"같은 선박 화물 중 {len(pairs)}쌍이 혼재 충돌입니다({', '.join(pairs)}) — "
                     "격리 적재 여부 확인이 필요합니다")
    # 머리에 물질 이름을 넣지 않는다 — 여러 종이면 이름 나열이 됐다(사용자 요청 9/29). 어느 화물인지는
    # 화물별 판정 줄과 확인 필요가 보여 준다.
    scope = f" · 화물 {len(focus)}종 종합" if len(focus) > 1 and ship_level is RiskLevel.SAFE else ""
    # 머리 뒤에서 줄을 바꾼다 — 한 줄에 붙어 있으면 머리와 본문이 구분되지 않았다(2026-09-29).
    return f"[혼재 판정 {ship_level.value}{scope}]\n" + ". ".join(parts) + "."


# 어느 화물에나 붙는 일반 수칙의 낱말. 프롬프트로 금지해도 모델이 계속 썼다(9/29 실측: 가솔린 체크리스트
# 4항목 중 "환기 상태를 점검", "금연 및 열점화원 제거" 2개). 벌크 하역에 '용기'도 맞지 않는다.
# [2026-09-29] '마개'·'개봉'도 용기 취급 말이다 — "개봉 전 마개가 조심스럽게 열렸는지 확인"(실측).
_GENERIC_CHECK_WORDS = ("보호구", "MSDS", "숙지", "폐기물", "환기", "방폭", "스파크", "점화원", "금연", "화기", "용기",
                        "마개", "개봉")


def _drop_generic_checklist(items: list[str], cargo_names: list[str]) -> list[str]:
    """일반 수칙 낱말이 있고 **숫자도 화물 이름도 없는** 항목을 뺀다 (2026-09-29).

    숫자(인화점·온도 등)나 화물 이름이 들어 있으면 이 화물에 맞춘 항목일 수 있어 남긴다.
    '확인할 것'에서 옮겨 온 항목(충돌·확인 필요)은 화물·이웃 이름이 들어 있어 빠지지 않는다.
    """
    def generic(item: str) -> bool:
        return (
            any(w in item for w in _GENERIC_CHECK_WORDS)
            and not any(ch.isdigit() for ch in item)
            and not any(n and n in item for n in cargo_names)
        )
    return [i for i in items if not generic(i)]


def _by_text(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(우리 화물 이름, 문장) 을 문장별로 묶어 ('가솔린·케로젠', 문장) 으로 — 같은 사유를 화물마다
    되풀이하지 않는다. 처음 나온 순서를 지킨다."""
    merged: dict[str, list[str]] = {}
    for name, text in items:
        names = merged.setdefault(text, [])
        if name not in names:
            names.append(name)
    return [("·".join(names), text) for text, names in merged.items()]


_BERTH_NO = re.compile(r"^(.+?)-(\d+)선석$")


def _join_berths(berths: list[str]) -> str:
    """['4부두-1선석', '4부두-2선석', 'SK6부두'] → '4부두 1·2선석, SK6부두' (처음 나온 순서)."""
    groups: dict[str, list[str]] = {}
    for b in dict.fromkeys(berths):
        m = _BERTH_NO.match(b)
        groups.setdefault(m.group(1) if m else b, []).append(m.group(2) if m else "")
    return ", ".join(
        f"{w} {'·'.join(sorted(nos, key=int))}선석" if all(nos) else w for w, nos in groups.items()
    )


def _verdict_basis(
    verdicts: list[_Verdict], adjacent_count: int, onboard: list[OnboardConflict],
) -> list[str]:
    """등급의 근거 — 화면이 등급 바로 아래 두는 줄들 (2026-09-29).

    _checked_facts 와 같은 사실을 한 줄씩 나눠, 등급을 만든 것부터 적는다. 검사를 못 한
    것은 여기 넣지 않고 _needs_check 로 보낸다(근거와 '못 본 것'을 섞지 않는다).

    화물을 여러 종 실은 배는 **모든 화물**의 근거를 모으고 어느 화물 얘기인지 이름을 붙인다.
    예전엔 대표 화물 것만 적어, 대표가 아닌 화물의 충돌은 화물별 등급 칩에만 보였다.
    """
    multi = len(verdicts) > 1
    lines: list[str] = []
    if onboard:
        pairs = sorted({f"{c.cargo_a_name}↔{c.cargo_b_name}" for c in onboard})
        lines.append(f"같은 선박 화물끼리 혼재 충돌 {len(pairs)}쌍({', '.join(pairs)}) — 격리 적재 확인 필요")
    conflicts: list[tuple[str, str]] = []
    packaging: list[tuple[str, str]] = []
    for v in verdicts:
        name = _cargo_display_name(v.target_row)
        # [2026-09-29] 이웃 화물 하나당 한 줄 — 예전엔 (이웃 선석 × 기준)마다 한 줄이라, 4부두 1·2선석의
        #   질산 하나가 MSDS·46 CFR 150 으로 4줄이 됐다(사용자 지적: 배지 아래 가독성). 선석은 묶고 기준은 ' / '.
        by_adj: dict[str, tuple[list[str], list[str]]] = {}
        for c in v.conflicts:
            berths, reasons = by_adj.setdefault(c.adjacent_name, ([], []))
            berths.append(c.adjacent_berth)
            reasons.append(f"MSDS '{c.shared_category}' 혼재금지")
        for c in v.bulk_compatibility_conflicts:
            berths, reasons = by_adj.setdefault(c.adjacent_name, ([], []))
            berths.append(c.adjacent_berth)
            reasons.append(
                f"46 CFR 150 {c.target_group_name}({c.target_group}) ↔ {c.adjacent_group_name}({c.adjacent_group})"
            )
        for adj_name, (berths, reasons) in by_adj.items():
            conflicts.append((name, f"{_join_berths(berths)} {adj_name} — {' / '.join(dict.fromkeys(reasons))}"))
        for p in v.packaging_violations:
            packaging.append((name, f"포장·하역방식 부적합: {p.reason}"))
    for who, text in _by_text(conflicts):
        lines.append(f"이웃 화물 충돌: {who} ↔ {text}" if multi else f"이웃 화물 충돌: {text}")
    for who, text in _by_text(packaging):
        lines.append(f"{who} · {text}" if multi else text)
    # 판정 근거가 없는 이웃 화물도 등급을 올린다(근거 없는 조합을 안전으로 보지 않는다) — 등급의
    # 이유이므로 여기 적는다. 빠뜨리면 '주의'인데 근거 줄은 "충돌 없음"뿐이었다(실측 SH SOPHIA).
    n_unassessed = len({p.adjacent_chem_id for v in verdicts for p in v.unassessed_pairs})
    if n_unassessed:
        lines.append(f"이웃 화물 {n_unassessed}종은 혼재 판정 근거가 없음 — 근거가 없는 조합은 안전으로 보지 않습니다")
    if adjacent_count == 0:
        lines.append("이웃 선석에 실린 화물 없음 — 혼재 비교 대상 없음")
    elif not conflicts:
        lines.append(
            "판정 근거가 있는 이웃 화물과는 MSDS·46 CFR 150 기준 충돌 없음" if n_unassessed
            else f"{_adjacent_label(verdicts, adjacent_count)}과 MSDS·46 CFR 150 기준 충돌 없음"
        )
    return lines


def _needs_check(verdicts: list[_Verdict]) -> list[str]:
    """확인 필요 — 판정에 쓰지 못한 근거를 '대상 — 이유' 로 (2026-09-29).

    판정 근거가 없는 이웃 화물은 한 줄로 묶는다. 사유가 대개 대상 화물 쪽 자료 공백이라 이웃마다
    같은 문장이 되풀이됐다(실측: 30건 × 4구절). 모든 쌍에 공통인 사유만 앞 두 구절 남긴다.
    화물을 여러 종 실은 배는 모든 화물에서 모으고, 해당 화물 이름을 앞에 붙인다(_verdict_basis 와 같다).
    """
    multi = len(verdicts) > 1

    def tag(vs: list[_Verdict], text: str) -> str:
        return f"{'·'.join(_cargo_display_name(v.target_row) for v in vs)} · {text}" if multi else text

    items: list[str] = []
    gap = [v for v in verdicts if v.unassessed_pairs]
    if gap:
        pairs = [p for v in gap for p in v.unassessed_pairs]
        names = list(dict.fromkeys(p.adjacent_name for p in pairs))
        reasons = [set(p.reason.split(" / ")) for p in pairs]
        common = [r for r in pairs[0].reason.split(" / ") if all(r in s for s in reasons)]
        who = ", ".join(names[:3]) + (f" 외 {len(names) - 3}종" if len(names) > 3 else "")
        items.append(tag(gap, f"이웃 화물 {who} — 혼재 판정 근거 부족"
                              + (f"({'; '.join(common[:2])})" if common else "")))
    # 하역방식은 용기등급 Ⅰ 화물만 판정한다(find_packing_violation). 그 밖의 화물은 알아도 등급이
    # 바뀌지 않는데, 하역방식을 넘기는 호출부가 없어 거의 모든 판정에 이 줄이 붙었다(2026-09-29).
    unchecked = [v for v in verdicts
                 if v.target_row.packing_group == PACKING_GROUP_HIGH_RISK
                 and not v.unload_method_name and not v.packaging_violations]
    if unchecked:
        items.append(tag(unchecked, "하역방식 — 신고가 없어 포장·하역 적합성은 보지 못했습니다"))
    return items


async def assess_safety(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    request: SafetyAssessmentRequest,
) -> SafetyAssessmentResult:
    """판정 + LLM 서술. 응답 형태는 종전과 같고 cargo_verdicts 만 늘었다.

    LLM 은 한 번만 부른다 — 화물마다 부르면 호출 비용이 화물 수만큼 늘어난다.
    [2026-09-29] 서술 대상은 대표 화물 하나가 아니라 _focus 다: 안전이 아닌 화물만, 모두 안전이면
    전 화물을 묶어서. target_cargo_name 도 그 화물들 이름이다(화면의 "○○ 기준").
    """
    v, verdicts, summaries, onboard, ship_level = await _compute_verdicts(db, neo4j_driver, request)
    needs_check = _needs_check(verdicts)
    basis = _verdict_basis(verdicts, len(request.adjacent_cargos), onboard)
    focus = _focus(verdicts, onboard)
    methods = {x.unload_method_name for x in focus}

    llm_result = await llm_client.generate_structured(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(
            target_cargo_name=_focus_name(focus),
            hazard_summary=_merged_hazards(focus),
            conflicts=[c for x in focus for c in x.conflicts],
            bulk_compatibility_conflicts=[c for x in focus for c in x.bulk_compatibility_conflicts],
            packaging_violations=[p for x in focus for p in x.packaging_violations],
            unassessed_pairs=[p for x in focus for p in x.unassessed_pairs],
            adjacent_count=len(request.adjacent_cargos),
            # 하나라도 신고가 없으면 '검사하지 않음'으로 넘긴다 — 본 것처럼 쓰지 않게.
            unload_method_name="·".join(sorted(methods)) if None not in methods else None,
            onboard_conflicts=onboard,
            needs_check=needs_check,
            # 모든 화물의 충돌을 화물 이름과 함께 넘긴다 — 대표 화물 것만 넘기면 나머지 화물의
            # 충돌이 체크리스트에서 빠졌다(2026-09-29).
            conflict_checks=[b for b in basis if b.startswith("이웃 화물 충돌") or "포장·하역방식 부적합" in b],
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
        target_cargo_name=_focus_name(focus),
        risk_level=ship_level,
        # 중합성 화물(IMDG ", STABILIZED")이면 탱크 온도·억제제 확인 항목을 규칙으로
        # 맨 앞에 붙인다 — LLM 이 뽑을지에 맡기지 않는다(stabilized_cargo.py, 2026-09-17).
        checklist=with_stabilized_checklist(
            _drop_generic_checklist(
                llm_result.checklist, [_cargo_display_name(x.target_row) for x in verdicts],
            ),
            v.target_row.un_no, v.target_row.cas_no,
        ),
        # [2026-09-29] 주요 위험성은 MSDS GHS 분류에서 코드가 만든다(_hazard_lines). 분류가 없는 화물뿐일
        #   때만 LLM 것을 쓴다. LLM 화물 설명(cargo_profile)은 쓰지 않는다 — 여러 화물이면 가장 센 물질의
        #   특성을 전부에 씌웠고(케로젠·디젤을 '고인화성'), 한 화물이어도 분류에 없는 위험을 썼다
        #   (가솔린 '피부에 심각한 자극' — 피부 자극 분류 없음). 사용자 결정 9/29.
        key_hazards=_hazard_lines(focus) or llm_result.key_hazards,
        # reasoning 은 종전 소비처(챗봇 근거·옛 화면)용으로 남긴다 — 등급의 이유(코드) + 화물 특성(LLM).
        reasoning="\n".join(
            s for s in (_checked_facts(focus, len(request.adjacent_cargos), onboard, ship_level),) if s
        ),
        cargo_profile="",
        adjacent_count=len(request.adjacent_cargos),
        verdict_basis=basis,
        needs_check=needs_check,
        conflicts=v.conflicts,
        # IMDG 항목은 **참고 정보**로 응답에 남긴다 — 판정 근거와 LLM 프롬프트
        # 에서는 빠졌지만, 관제사가 "이 조합이 국제 규정상 선내 격리 대상인가"를
        # 알고 싶을 수 있고 화면이 그 사실을 표시한다.
        imdg_conflicts=v.imdg_conflicts,
        imdg_unconfirmed_pairs=v.imdg_unconfirmed_pairs,
        bulk_compatibility_conflicts=v.bulk_compatibility_conflicts,
        packaging_violations=v.packaging_violations,
        unassessed_pairs=v.unassessed_pairs,
        rule_engine_floor=ship_level,
        msds_sections_used=list(v.hazard_summary.keys()),
        imdg_classes=v.imdg_classes,
        cargo_verdicts=summaries,
        onboard_conflicts=onboard,
    )
