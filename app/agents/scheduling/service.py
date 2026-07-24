"""스케줄링 에이전트 오케스트레이션.

흐름: 화물 조회 -> cargo_category 확인 -> Neo4j로 수심/화물적합 선석 후보 탐색
-> Postgres로 요청 시간대 점유 여부 확인 -> 인접 선석 취급 카테고리로
adjacent_cargos 근사 채움 -> 여유 우선·수심여유 큰 순 정렬 -> 상위 3개 반환.

LLM을 쓰지 않는다 — 계획서상 스케줄링 에이전트는 Neo4j Cypher 쿼리 기반의
결정적 판단이고(안전관제 에이전트만 MSDS 근거 문장 생성에 LLM을 씀), 최종
자연어 종합 판단은 상위 오케스트레이터(이번 범위 밖)의 몫이다.
"""

from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.msds_context import resolve_cargo
from app.agents.safety.schemas import AdjacentCargo, CargoRef
from app.core.exceptions import CargoCategoryUnknownError
from app.models import MsdsChemical

from .category_map import representative_chem_id
from .graph_queries import find_adjacent_categories, find_eligible_berths, get_chemical_category
from .occupancy import find_overlapping_port_calls
from .schemas import (
    BerthCandidate,
    ConflictingPortCall,
    OccupancyStatus,
    SchedulingRequest,
    SchedulingResult,
)

MAX_CANDIDATES = 3


def _cargo_display_name(row: MsdsChemical) -> str:
    return row.name_ko or row.name_en or row.chem_id


def _adjacent_cargos_for(categories_by_neighbor: list[dict]) -> list[AdjacentCargo]:
    """인접 선석별 취급 카테고리를 대표 화학물질로 근사해 AdjacentCargo 목록을 만든다."""
    adjacent_cargos: list[AdjacentCargo] = []
    for entry in categories_by_neighbor:
        for category in entry["categories"]:
            chem_id = representative_chem_id(category)
            if chem_id is None:
                continue
            adjacent_cargos.append(
                AdjacentCargo(berth_name=entry["adjacent_berth_id"], cargo=CargoRef(chem_id=chem_id))
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
                adjacent_cargos=_adjacent_cargos_for(adjacency_map.get(row["berth_id"], [])),
            )
        )

    # 여유 선석 우선, 그 다음 수심 여유가 큰 순(안전 마진이 큰 순)
    candidates.sort(key=lambda c: (c.occupancy_status is OccupancyStatus.OCCUPIED, -c.draught_margin_m))

    top_candidates = candidates[:MAX_CANDIDATES]
    for rank, candidate in enumerate(top_candidates, start=1):
        candidate.rank = rank

    return SchedulingResult(
        target_cargo_name=_cargo_display_name(target_row),
        cargo_category=category,
        candidates=top_candidates,
        total_eligible_count=len(eligible),
    )
