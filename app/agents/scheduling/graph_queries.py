"""Neo4j 기반 선석 후보 탐색.

data-pipeline/data_pipeline/loaders/berth_neo4j_loader.py 와
cargo_category_loader.py 가 적재한 그래프를 조회한다:
  (:Chemical {cargo_category})
  (:Berth {depth_m, berth_group, ...})-[:HANDLES]->(:CargoCategory {name})
  (:Berth)-[:ADJACENT_TO]->(:Berth)
  (:Berth)-[:SUBSTITUTABLE_WITH {shared_products, to_max_dwt, to_depth_m}]->(:Berth)  (온산 MVP 이식)
  (:Anchorage {id, name, tonnage_rule, tonnage_lower, tonnage_upper, anchorage_type,
               latitude, longitude})  (온산 MVP 이식)

Anchorage 톤수 배정은 (:Berth)-[:FALLBACK_ANCHORAGE]->(:Anchorage) 정적 간선을 쓰지
않는다 — 그 간선은 부두의 "통상" 취급 톤수로 그래프 적재 시점에 미리 계산해 둔
것이라, 실제 요청 선박의 DWT와 다를 수 있다(9장 비교분석 지적사항). 대신
find_anchorage_candidates()로 전체 Anchorage를 가져와 select_anchorage_for_dwt()가
매 요청마다 실제 선박 DWT로 동적 매칭한다(팀원 원본 assign_anchorage(dwt=...)와
동일한 방식).
"""

from neo4j import AsyncDriver

_CYPHER_GET_CHEMICAL_CATEGORY = """
MATCH (c:Chemical {id: $chem_id})
RETURN c.cargo_category AS category
"""

_CYPHER_FIND_ELIGIBLE_BERTHS = """
MATCH (b:Berth)-[:HANDLES]->(:CargoCategory {name: $category})
WHERE b.depth_m IS NOT NULL AND b.depth_m >= $min_depth
RETURN b.id AS berth_id, b.wharf_name AS wharf_name, b.port_name AS port_name,
       b.depth_m AS depth_m, b.berth_group AS berth_group
ORDER BY b.depth_m DESC
"""

# 온산 MVP(feature/onsan-mvp) 이식: 전용 선석이 점유 중일 때 같은 운영사/파이프라인
# 안에서 대체 가능한 선석을 찾는다(build_substitutability.py의 SUBSTITUTABLE_WITH
# 관계). ADJACENT_TO(물리적 인접 = 혼재위험)와는 완전히 별도 관계다.
_CYPHER_FIND_SUBSTITUTABLE_BERTHS = """
MATCH (b:Berth {id: $berth_id})-[r:SUBSTITUTABLE_WITH]->(target:Berth)
RETURN target.id AS berth_id, target.wharf_name AS wharf_name, target.port_name AS port_name,
       target.depth_m AS depth_m, target.berth_group AS berth_group,
       r.shared_products AS shared_products, r.to_max_dwt AS to_max_dwt, r.to_depth_m AS to_depth_m
"""

# 대체도 없을 때(단독선석 또는 대체 후보 전부 점유) 정박지 후보 전체를 가져와
# select_anchorage_for_dwt()가 실제 선박 DWT로 그중 하나를 고른다. 벙커링 전용
# (BUNKER_RING)은 급유 목적이라 일반 접안 대기 후보에서 제외한다(팀원 모델의
# E/W 계열만 정박지 대기로 쓰는 것과 동일 구분).
_CYPHER_FIND_ANCHORAGE_CANDIDATES = """
MATCH (a:Anchorage)
WHERE a.anchorage_type IN ['POLYGON', 'CIRCLE']
RETURN a.id AS anchorage_id, a.name AS name, a.tonnage_rule AS tonnage_rule,
       a.tonnage_lower AS tonnage_lower, a.tonnage_upper AS tonnage_upper,
       a.latitude AS latitude, a.longitude AS longitude
"""

_CYPHER_FIND_ADJACENT_CATEGORIES = """
MATCH (b:Berth)-[:ADJACENT_TO]->(n:Berth)-[:HANDLES]->(cat:CargoCategory)
WHERE b.id IN $berth_ids
RETURN b.id AS berth_id, n.id AS adjacent_berth_id, collect(DISTINCT cat.name) AS categories
"""


async def get_chemical_category(driver: AsyncDriver, chem_id: str) -> str | None:
    """Chemical.cargo_category 속성값을 조회한다. 노드가 없거나 속성 미설정이면 None."""
    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_GET_CHEMICAL_CATEGORY, chem_id=chem_id)
            record = await result.single()
            return record["category"] if record else None

        return await session.execute_read(_tx)


async def find_eligible_berths(
    driver: AsyncDriver,
    *,
    category: str,
    min_depth: float,
) -> list[dict]:
    """화물 카테고리를 취급하고 수심 조건(min_depth 이상)을 만족하는 선석 목록.

    depth_m이 NULL인 선석은 안전 판단이 불가능하므로 결과에서 제외한다
    (모르면 추천하지 않는다).
    """
    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(
                _CYPHER_FIND_ELIGIBLE_BERTHS,
                category=category,
                min_depth=min_depth,
            )
            return [record.data() async for record in result]

        return await session.execute_read(_tx)


async def find_adjacent_categories(
    driver: AsyncDriver,
    *,
    berth_ids: list[str],
) -> dict[str, list[dict]]:
    """각 후보 선석의 인접 선석과, 그 인접 선석이 취급하는 카테고리 목록.

    Returns:
        { berth_id: [{"adjacent_berth_id": ..., "categories": [...]}, ...] }
    """
    if not berth_ids:
        return {}

    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_FIND_ADJACENT_CATEGORIES, berth_ids=berth_ids)
            return [record.data() async for record in result]

        rows = await session.execute_read(_tx)

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["berth_id"], []).append(
            {"adjacent_berth_id": row["adjacent_berth_id"], "categories": row["categories"]}
        )
    return grouped


async def find_substitutable_berths(driver: AsyncDriver, *, berth_id: str) -> list[dict]:
    """berth_id가 점유 중일 때 시도해볼 대체 선석 후보 목록(같은 운영사/파이프라인 한정)."""
    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_FIND_SUBSTITUTABLE_BERTHS, berth_id=berth_id)
            return [record.data() async for record in result]

        return await session.execute_read(_tx)


async def find_anchorage_candidates(driver: AsyncDriver) -> list[dict]:
    """일반 접안 대기용 정박지 전체 목록(벙커링 전용 제외). select_anchorage_for_dwt에 넘긴다."""
    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_FIND_ANCHORAGE_CANDIDATES)
            return [record.data() async for record in result]

        return await session.execute_read(_tx)


# VLCC급(DWT 15만톤 이상)이거나 DWT를 모르면 정박지를 고르지 않는다(온산 MVP
# 이식: data-pipeline/berth_neo4j_loader.py의 assign_fallback_anchorage와 동일
# 임계값·로직 — VLCC는 별도 부이/외해 대기가 필요하고, 이 그래프의 정박지
# 노드로는 표현되지 않는다).
VLCC_BUOY_DWT = 150_000


def select_anchorage_for_dwt(dwt_t: float | None, anchorages: list[dict]) -> dict | None:
    """실제 선박 DWT에 맞는 정박지 하나를 고른다(팀원 원본 assign_anchorage(dwt=...)와 동일 로직).

    상한(tonnage_upper)이 있으면 그 이내, 하한(tonnage_lower)만 있으면(예: E3
    "2만톤 이상") 그 이상인 정박지도 후보에 포함한다. 후보가 여럿이면 상한이
    있는(더 타이트한) 쪽을 우선한다.
    """
    if dwt_t is None or dwt_t >= VLCC_BUOY_DWT:
        return None
    candidates = [
        a
        for a in anchorages
        if (a.get("tonnage_lower") is not None or a.get("tonnage_upper") is not None)
        and (a["tonnage_upper"] is None or dwt_t <= a["tonnage_upper"])
        and (a["tonnage_lower"] is None or dwt_t >= a["tonnage_lower"])
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda a: a["tonnage_upper"] if a["tonnage_upper"] is not None else float("inf"))
