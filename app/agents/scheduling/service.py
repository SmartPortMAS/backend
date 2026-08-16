"""스케줄링 에이전트 오케스트레이션.

흐름: 화물 조회 -> cargo_category 확인 -> Neo4j로 수심/화물적합 선석 후보 탐색
-> Postgres로 요청 시간대 점유 여부 확인 -> 인접 선석 취급 카테고리로
adjacent_cargos 근사 채움 -> 여유 우선·수심여유 큰 순 정렬 -> 상위 3개 반환.

LLM을 쓰지 않는다 — 계획서상 스케줄링 에이전트는 Neo4j Cypher 쿼리 기반의
결정적 판단이고(안전관제 에이전트만 MSDS 근거 문장 생성에 LLM을 씀), 최종
자연어 종합 판단은 상위 오케스트레이터(이번 범위 밖)의 몫이다.

온산 MVP(feature/onsan-mvp) 이식: resolve_berth_assignment()가 "전용 -> 같은
운영사/파이프라인 대체(SUBSTITUTABLE_WITH) -> 톤수 맞는 정박지 대기
(FALLBACK_ANCHORAGE)" 3단계 배정을 담당한다. find_berth_candidates()가 만드는
"카테고리 적합 상위 3순위" 흐름과는 별개로, 오케스트레이터가 특정 후보(대개
1순위)가 점유 중일 때 호출하는 보조 경로다.
"""

from datetime import datetime

from neo4j import AsyncDriver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.msds_context import resolve_cargo
from app.agents.safety.schemas import AdjacentCargo, CargoRef
from app.core.exceptions import CargoCategoryUnknownError
from app.models import MsdsChemical

from .category_map import representative_chem_id
from .graph_queries import (
    VLCC_BUOY_DWT,
    find_adjacent_categories,
    find_anchorage_candidates,
    find_eligible_berths,
    find_substitutable_berths,
    get_chemical_category,
    select_anchorage_for_dwt,
)
from .occupancy import find_overlapping_port_calls
from .schemas import (
    AnchorageAssignment,
    BerthCandidate,
    BerthResolution,
    ConflictingPortCall,
    OccupancyStatus,
    SchedulingRequest,
    SchedulingResult,
    VesselSpec,
)

MAX_CANDIDATES = 3


def _cargo_display_name(row: MsdsChemical) -> str:
    return row.name_ko or row.name_en or row.chem_id


_QUERY_REAL_ADJACENT_CARGO = text("""
    -- mart.berth_current_cargo(실제 재항 화물) -> facility_alias -> wharf_name.
    --
    -- facility_alias의 사전(source_names)이 upa_port_call(VTS 원문)과
    -- upa_cargo_manifest(합성 화물의 자체 표기, 예: 'S-Oil 1부두') 둘 다 포함한다
    -- (mart_views.sql 0-B절) — berth_current_cargo.facility_name이
    -- COALESCE(cm.facility_name, ip.facility_name)라 두 어휘가 섞여 나오는데,
    -- 사전이 이제 둘 다 알고 있으므로 단일 조인으로 끝난다(예전엔 여기서
    -- COALESCE 폴백 정규화를 따로 했었는데, 근본 원인을 사전 쪽에서 없앴다).
    SELECT fa.wharf_name, bcc.chem_id, bcc.cas_no
    FROM mart.berth_current_cargo bcc
    JOIN mart.facility_alias fa
      ON fa.source_name = bcc.facility_name AND fa.facility_type = 'BERTH'
    WHERE bcc.chem_id IS NOT NULL
      AND fa.wharf_name = ANY(CAST(:wharf_names AS text[]))
""")


async def _real_adjacent_cargo_by_wharf(
    db: AsyncSession, wharf_names: list[str]
) -> dict[str, list[dict]]:
    """mart.berth_current_cargo에서 실제 재항 화물을 wharf_name별로 조회.

    chem_id가 NULL인 행(위험물인데 정체 미확인, UN번호 결측 등)은 CargoRef를
    만들 수 없어 제외한다 — safety/schemas.py의 CargoRef 제약과 동일 이유
    (V-DG-01과 같은 성격의 한계: 식별 불가 화물은 애초에 판정 입력이 안 된다).
    """
    if not wharf_names:
        return {}
    rows = (
        await db.execute(_QUERY_REAL_ADJACENT_CARGO, {"wharf_names": wharf_names})
    ).mappings().all()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["wharf_name"], []).append(
            {"chem_id": row["chem_id"], "cas_no": row["cas_no"]}
        )
    return grouped


def _adjacent_cargos_for(
    categories_by_neighbor: list[dict], real_cargo_by_wharf: dict[str, list[dict]]
) -> list[AdjacentCargo]:
    """인접 선석별 화물을 채운다.

    mart.berth_current_cargo(실제 재항 화물)가 있으면 그걸 쓰고, 없으면(화물
    manifest가 아직 합성 데이터 위주라 커버리지가 낮음·재항 선박 없음·facility_alias
    매칭 실패 등) 카테고리 대표값으로 근사한다 — category_map.py의 원래 설계를
    완전히 버리지 않고 폴백으로 남긴 이유는, "실데이터가 없다"를 "위험이 없다"로
    착각하면 안전 판정을 낙관적으로 왜곡하기 때문이다. 실데이터가 있으면 그게
    항상 우선한다 — 근사값보다 신뢰도가 높다(cargo_msds ★ 안전관제 핵심 뷰 참고).
    """
    adjacent_cargos: list[AdjacentCargo] = []
    for entry in categories_by_neighbor:
        real_cargos = real_cargo_by_wharf.get(entry.get("adjacent_wharf_name") or "", [])
        if real_cargos:
            for rc in real_cargos:
                adjacent_cargos.append(
                    AdjacentCargo(
                        berth_name=entry["adjacent_berth_id"],
                        cargo=CargoRef(chem_id=rc["chem_id"], cas_no=rc["cas_no"]),
                        distance_m=entry.get("distance_m"),
                    )
                )
            continue
        for category in entry["categories"]:
            chem_id = representative_chem_id(category)
            if chem_id is None:
                continue
            adjacent_cargos.append(
                AdjacentCargo(
                    berth_name=entry["adjacent_berth_id"],
                    cargo=CargoRef(chem_id=chem_id),
                    distance_m=entry.get("distance_m"),
                )
            )
    return adjacent_cargos


async def find_berth_candidates(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    request: SchedulingRequest,
) -> SchedulingResult:
    target_row = await resolve_cargo(db, request.cargo)

    category = await get_chemical_category(neo4j_driver, target_row.chem_id)
    if not category:
        raise CargoCategoryUnknownError(target_row.chem_id)

    min_depth = request.vessel.draught_m + request.draught_margin_m
    eligible = await find_eligible_berths(neo4j_driver, category=category, min_depth=min_depth)

    if not eligible:
        return SchedulingResult(
            target_cargo_name=_cargo_display_name(target_row),
            cargo_category=category,
            candidates=[],
            total_eligible_count=0,
        )

    berth_ids = [row["berth_id"] for row in eligible]
    wharf_names = [row["wharf_name"] for row in eligible]

    occupancy_map = await find_overlapping_port_calls(
        db,
        wharf_names=wharf_names,
        window_start=request.window_start,
        window_end=request.window_end,
    )
    adjacency_map = await find_adjacent_categories(neo4j_driver, berth_ids=berth_ids)
    adjacent_wharf_names = list({
        entry["adjacent_wharf_name"]
        for entries in adjacency_map.values()
        for entry in entries
        if entry.get("adjacent_wharf_name")
    })
    real_cargo_by_wharf = await _real_adjacent_cargo_by_wharf(db, adjacent_wharf_names)

    candidates: list[BerthCandidate] = []
    for row in eligible:
        conflicts = occupancy_map.get(row["wharf_name"], [])
        status = OccupancyStatus.OCCUPIED if conflicts else OccupancyStatus.AVAILABLE
        candidates.append(
            BerthCandidate(
                rank=0,  # 정렬 후 채움
                berth_id=row["berth_id"],
                wharf_name=row["wharf_name"],
                port_name=row["port_name"],
                depth_m=row["depth_m"],
                berth_group=row.get("berth_group"),
                onsan_scope=bool(row.get("onsan_scope")),
                draught_margin_m=row["depth_m"] - request.vessel.draught_m,
                occupancy_status=status,
                conflicting_port_calls=[
                    ConflictingPortCall(
                        vessel_name=c["vessel_name"],
                        arrival_at_utc=c["arrival_at_utc"],
                        departure_at_utc=c["departure_at_utc"],
                    )
                    for c in conflicts
                ],
                adjacent_cargos=_adjacent_cargos_for(
                    adjacency_map.get(row["berth_id"], []), real_cargo_by_wharf
                ),
            )
        )

    # 온산 스코프 우선 → 여유 선석 우선 → 수심 여유가 큰 순(안전 마진이 큰 순).
    #
    # 온산을 첫 키로 둔 것은 팀원 P1의 "스케줄링 후보풀 온산 제한" 요구사항을 하드
    # 필터 대신 정렬로 구현한 것이다. 필터로 거르면 온산에 적합 선석이 MAX_CANDIDATES
    # 미만인 화물에서 후보가 줄거나 사라진다 — 원유는 온산 부이 2기가 전부라
    # 3순위 자리를 채울 수 없다. 정렬이면 온산이 채울 수 있는 만큼 앞을 차지하고,
    # 모자란 자리만 스코프 밖(울산본항/신항)이 이어받는다.
    #
    # 온산 후보가 3개 이상이면 하드 필터와 결과가 완전히 같다 — 상위 3개가 잘리기
    # 전에 온산이 다 차지하기 때문이다(2026-08-02 실측: 액체화학·유류 시나리오에서
    # 하드/소프트 결과 동일, 원유에서만 3순위 폴백 유무가 갈림).
    candidates.sort(
        key=lambda c: (
            not c.onsan_scope,
            c.occupancy_status is OccupancyStatus.OCCUPIED,
            -c.draught_margin_m,
        )
    )

    top_candidates = candidates[:MAX_CANDIDATES]
    for rank, candidate in enumerate(top_candidates, start=1):
        candidate.rank = rank

    return SchedulingResult(
        target_cargo_name=_cargo_display_name(target_row),
        cargo_category=category,
        candidates=top_candidates,
        total_eligible_count=len(eligible),
    )


async def resolve_berth_assignment(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    *,
    candidate: BerthCandidate,
    vessel: VesselSpec,
    window_start: datetime,
    window_end: datetime,
) -> BerthResolution:
    """전용 선석(candidate)이 점유 중일 때 대체 -> 정박지 3단계로 재배정을 시도한다.

    온산 MVP(feature/onsan-mvp) 이식: 액체화물 부두는 파이프라인이 특정 탱크단지로
    고정 연결된 전용부두라, 대체는 같은 운영사(SUBSTITUTABLE_WITH) 안에서만 가능하고
    그마저 없으면 실제 선박 DWT에 맞는 정박지 대기가 현실이다(select_anchorage_for_dwt
    — onsan_mvp/scripts/build_substitutability.py, build_anchorage_assignment.py의
    assign_anchorage(dwt=...)와 동일 로직).

    candidate가 이미 여유(AVAILABLE) 상태면 전용 선석을 그대로 확정한다 — 이
    함수는 "점유 중일 때만" 재탐색이 의미가 있다.
    """
    if candidate.occupancy_status is not OccupancyStatus.OCCUPIED:
        return BerthResolution(
            path="전용",
            berth=candidate,
            trace=[f"전용 선석 '{candidate.wharf_name}' 사용 가능"],
        )

    trace = [f"전용 선석 '{candidate.wharf_name}' 점유 중 (현재 접안 중인 선박 있음)"]
    substitutes = await find_substitutable_berths(neo4j_driver, berth_id=candidate.berth_id)

    for sub in substitutes:
        if sub.get("to_depth_m") is not None and vessel.draught_m > sub["to_depth_m"]:
            trace.append(f"대체 후보 '{sub['wharf_name']}' 탈락: 흘수 {vessel.draught_m}m > 수심 {sub['to_depth_m']}m")
            continue
        if (
            sub.get("to_max_dwt") is not None
            and vessel.dwt_t is not None
            and vessel.dwt_t > sub["to_max_dwt"]
        ):
            trace.append(f"대체 후보 '{sub['wharf_name']}' 탈락: DWT {vessel.dwt_t} > 최대 {sub['to_max_dwt']}")
            continue

        occ = await find_overlapping_port_calls(
            db, wharf_names=[sub["wharf_name"]], window_start=window_start, window_end=window_end
        )
        if occ.get(sub["wharf_name"]):
            trace.append(f"대체 후보 '{sub['wharf_name']}'도 점유 중 - 다음 대체 탐색")
            continue

        trace.append(f"'{sub['wharf_name']}'(으)로 대체 배정 (공유화물: {sub.get('shared_products')})")
        substitute_candidate = BerthCandidate(
            rank=candidate.rank,
            berth_id=sub["berth_id"],
            wharf_name=sub["wharf_name"],
            port_name=sub.get("port_name"),
            depth_m=sub["depth_m"],
            berth_group=sub.get("berth_group"),
            onsan_scope=bool(sub.get("onsan_scope")),
            draught_margin_m=sub["depth_m"] - vessel.draught_m,
            occupancy_status=OccupancyStatus.AVAILABLE,
            adjacent_cargos=candidate.adjacent_cargos,
        )
        return BerthResolution(path="대체", berth=substitute_candidate, trace=trace)

    if substitutes:
        trace.append("대체 후보 전부 탈락(제원 초과 또는 점유 중) -> 정박지 대기 탐색")
    else:
        trace.append("대체 가능 선석 없음(단독선석) -> 정박지 대기 탐색")

    anchorage_candidates = await find_anchorage_candidates(neo4j_driver)
    anchorage_row = select_anchorage_for_dwt(vessel.dwt_t, anchorage_candidates)
    if anchorage_row is None:
        if vessel.dwt_t is None:
            trace.append("선박 DWT 미상 -> 톤수별 정박지 매칭 불가 -> 배정불가")
        elif vessel.dwt_t >= VLCC_BUOY_DWT:
            trace.append(f"DWT {vessel.dwt_t} >= {VLCC_BUOY_DWT}(VLCC급) -> 정박지 대신 부이/외해 대기 필요 -> 배정불가")
        else:
            trace.append(f"DWT {vessel.dwt_t}에 맞는 정박지 없음 -> 배정불가")
        return BerthResolution(path="배정불가", trace=trace)

    trace.append(f"정박지 '{anchorage_row['name']}' 대기 배정")
    return BerthResolution(
        path="정박지대기",
        anchorage=AnchorageAssignment(
            anchorage_id=anchorage_row["anchorage_id"],
            name=anchorage_row["name"],
            tonnage_rule=anchorage_row.get("tonnage_rule"),
            latitude=anchorage_row.get("latitude"),
            longitude=anchorage_row.get("longitude"),
        ),
        trace=trace,
    )
