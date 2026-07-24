"""Neo4j 기반 선석 후보 탐색.

data-pipeline/data_pipeline/loaders/berth_neo4j_loader.py 와
cargo_category_loader.py 가 적재한 그래프를 조회한다:
  (:Chemical {cargo_category})
  (:Berth {depth_m, ...})-[:HANDLES]->(:CargoCategory {name})
  (:Berth)-[:ADJACENT_TO]->(:Berth)
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
       b.depth_m AS depth_m
ORDER BY b.depth_m DESC
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
