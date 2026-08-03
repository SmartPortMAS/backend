"""챗봇용 Neo4j 조회.

안전관제 에이전트(app/agents/safety/graph_queries.py)의 쿼리는 "대상 화물 1 : 인접
화물 N"의 충돌 탐지에 특화되어 있어 그대로는 재사용할 수 없다. 챗봇은 그 외에
(1) 단일 화물 프로필, (2) 특정 화물의 혼재금지 카테고리와 그에 속하는 등재 화물
목록을 조회해야 하므로 여기에 따로 둔다. 혼재 "판정"은 여전히 safety 에이전트가
하고, 이 모듈은 설명에 필요한 사실만 모은다.

그래프 구조상 Chemical→Chemical 직접 관계는 없고 IncompatibleMaterial 카테고리
노드를 경유한다(data-pipeline/loaders/msds_neo4j_loader.py 참고). 그리고 방향이
비대칭이라 두 방향을 모두 봐야 한다:
  A -[:INCOMPATIBLE_WITH]-> (카테고리) <-[:IS_CLASSIFIED_AS]- B   (A가 B류를 기피)
  A -[:IS_CLASSIFIED_AS]-> (카테고리) <-[:INCOMPATIBLE_WITH]- B   (B가 A류를 기피)
"""

from neo4j import AsyncDriver

_CYPHER_PROFILES = """
UNWIND $chem_ids AS cid
MATCH (c:Chemical {id: cid})
OPTIONAL MATCH (c)-[:HAS_HAZARD]->(h:HazardClass)
OPTIONAL MATCH (c)-[:INCOMPATIBLE_WITH]->(m:IncompatibleMaterial)
OPTIONAL MATCH (c)-[:IS_CLASSIFIED_AS]->(k:IncompatibleMaterial)
OPTIONAL MATCH (c)-[:HAS_IMDG_CLASS]->(ic:ImdgClass)
RETURN c.id      AS chem_id,
       c.name_ko AS name_ko,
       c.name_en AS name_en,
       c.cas_no  AS cas_no,
       c.un_no   AS un_no,
       collect(DISTINCT h.name)  AS hazard_classes,
       collect(DISTINCT m.name)  AS incompatible_categories,
       collect(DISTINCT k.name)  AS classified_as,
       collect(DISTINCT ic.code) AS imdg_classes
"""

# 대상 화물이 기피하는(INCOMPATIBLE_WITH) 카테고리 → 그 카테고리로 분류된 등재 화물.
# collect()는 null을 버리므로 해당 화물이 하나도 없는 카테고리는 빈 리스트가 된다
# (그래도 카테고리 자체는 답변에 필요한 정보라 행은 남긴다).
_CYPHER_AVOIDS = """
MATCH (c:Chemical {id: $chem_id})-[:INCOMPATIBLE_WITH]->(m:IncompatibleMaterial)
OPTIONAL MATCH (o:Chemical)-[:IS_CLASSIFIED_AS]->(m)
WHERE o.id <> $chem_id
WITH m.name AS category, collect(DISTINCT o) AS others
RETURN category,
       [o IN others | {chem_id: o.id, name_ko: o.name_ko, name_en: o.name_en, cas_no: o.cas_no}]
         AS chemicals
"""

# 반대 방향 — 대상 화물이 속한 카테고리를 기피한다고 명시한 화물들.
_CYPHER_AVOIDED_BY = """
MATCH (c:Chemical {id: $chem_id})-[:IS_CLASSIFIED_AS]->(m:IncompatibleMaterial)
OPTIONAL MATCH (o:Chemical)-[:INCOMPATIBLE_WITH]->(m)
WHERE o.id <> $chem_id
WITH m.name AS category, collect(DISTINCT o) AS others
RETURN category,
       [o IN others | {chem_id: o.id, name_ko: o.name_ko, name_en: o.name_en, cas_no: o.cas_no}]
         AS chemicals
"""

_CYPHER_ALL_CHEMICALS = """
MATCH (c:Chemical)
RETURN c.id AS chem_id, c.name_ko AS name_ko, c.name_en AS name_en,
       c.cas_no AS cas_no, c.un_no AS un_no
ORDER BY c.name_ko
"""


async def _read(driver: AsyncDriver, cypher: str, **params) -> list[dict]:
    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(cypher, **params)
            return [record.data() async for record in result]

        return await session.execute_read(_tx)


async def fetch_profiles(driver: AsyncDriver, chem_ids: list[str]) -> dict[str, dict]:
    """chem_id → 프로필 dict. 그래프에 없는 chem_id는 결과에서 빠진다.

    빠졌다는 사실 자체가 중요한 신호다 — PostgreSQL(msds_chemical)에는 있지만
    Neo4j 적재 이후 KOSHA API로 lazy-fetch된 화물은 그래프에 없어서, 혼재금지
    관계를 "없음"이 아니라 "판정 불가"로 다뤄야 한다(service.py에서 처리).
    """
    if not chem_ids:
        return {}
    rows = await _read(driver, _CYPHER_PROFILES, chem_ids=chem_ids)
    return {row["chem_id"]: row for row in rows}


async def fetch_incompatible_groups(driver: AsyncDriver, chem_id: str) -> list[dict]:
    """양방향 혼재금지 카테고리 목록을 카테고리 기준으로 병합해 반환한다.

    반환: [{"category": str, "chemicals": [{chem_id, name_ko, name_en, cas_no}, ...]}, ...]
    """
    avoids = await _read(driver, _CYPHER_AVOIDS, chem_id=chem_id)
    avoided_by = await _read(driver, _CYPHER_AVOIDED_BY, chem_id=chem_id)

    merged: dict[str, dict[str, dict]] = {}
    for row in [*avoids, *avoided_by]:
        bucket = merged.setdefault(row["category"], {})
        for chem in row["chemicals"]:
            bucket[chem["chem_id"]] = chem

    return [
        {"category": category, "chemicals": sorted(chems.values(), key=lambda c: c["chem_id"])}
        for category, chems in sorted(merged.items())
    ]


async def fetch_all_chemicals(driver: AsyncDriver) -> list[dict]:
    return await _read(driver, _CYPHER_ALL_CHEMICALS)
