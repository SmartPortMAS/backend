"""Neo4j 지식그래프 기반 혼재금지 충돌 탐색.

data-pipeline/data_pipeline/loaders/msds_neo4j_loader.py 가 적재한 그래프를 그대로
조회한다 (Chemical -[:INCOMPATIBLE_WITH]-> IncompatibleMaterial <-[:IS_CLASSIFIED_AS]- Chemical).

두 방향을 모두 조회해야 하는 이유: 화학물질 A가 "이 물질은 강산류와 상극이다"라고
INCOMPATIBLE_WITH 관계로 자신의 MSDS(J코드)에 명시하는 것과, 화학물질 B 자신이
"나는 강산류다"라고 IS_CLASSIFIED_AS로 분류되는 것은 서로 다른 화학물질에서
비대칭적으로 생성된다. 예: 벤젠은 "강산류"를 INCOMPATIBLE_WITH로 갖고, 황산은
"강산류"를 IS_CLASSIFIED_AS로 갖는다 (msds_neo4j_loader.py 상단 주석 참고).
"""

from neo4j import AsyncDriver

_CYPHER_FIND_CONFLICTS = """
MATCH (a:Chemical {id: $target})-[:INCOMPATIBLE_WITH]->(m)<-[:IS_CLASSIFIED_AS]-(b:Chemical)
WHERE b.id IN $adjacent
RETURN b.id AS chem_id, b.name_ko AS name_ko, m.name AS category,
       'target_incompatible_with_adjacent' AS direction
UNION
MATCH (b:Chemical)-[:INCOMPATIBLE_WITH]->(m)<-[:IS_CLASSIFIED_AS]-(a:Chemical {id: $target})
WHERE b.id IN $adjacent
RETURN b.id AS chem_id, b.name_ko AS name_ko, m.name AS category,
       'adjacent_incompatible_with_target' AS direction
"""


async def find_incompatible_conflicts(
    driver: AsyncDriver,
    *,
    target_chem_id: str,
    adjacent_chem_ids: list[str],
) -> list[dict]:
    if not adjacent_chem_ids:
        return []

    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(
                _CYPHER_FIND_CONFLICTS,
                target=target_chem_id,
                adjacent=adjacent_chem_ids,
            )
            return [record.data() async for record in result]

        return await session.execute_read(_tx)


# ─────────────────────────────────────────────────────────────────────────────
# IMDG Code 공인 일반 격리표 기반 충돌 탐색 (data-pipeline의
# imdg_segregation_loader.py가 적재한 그래프). find_incompatible_conflicts와는
# 근거가 다른 별도 신호다 — 저건 MSDS 텍스트 마이닝, 이건 화물 대분류(Class)
# 간 IMDG Chapter 7.2 공인 규정. SEGREGATE 관계는 로더가 양방향으로 적재해서
# 한 방향 MATCH만으로 충분하다 (INCOMPATIBLE_WITH처럼 UNION 불필요).
# ─────────────────────────────────────────────────────────────────────────────

_CYPHER_FIND_IMDG_CONFLICTS = """
MATCH (a:Chemical {id: $target})-[:HAS_IMDG_CLASS]->(ca:ImdgClass)
MATCH (b:Chemical)-[:HAS_IMDG_CLASS]->(cb:ImdgClass)
WHERE b.id IN $adjacent
MATCH (ca)-[s:SEGREGATE]->(cb)
RETURN b.id AS chem_id, b.name_ko AS name_ko,
       ca.code AS target_class, cb.code AS adjacent_class,
       s.code AS segregation_code
"""


async def find_imdg_segregation_conflicts(
    driver: AsyncDriver,
    *,
    target_chem_id: str,
    adjacent_chem_ids: list[str],
) -> list[dict]:
    """대상 화물과 인접 화물들의 IMDG Class 간 공인 격리표 충돌을 탐색한다.

    대상 또는 인접 화물에 IMDG Class가 없거나(HAS_IMDG_CLASS 관계 없음),
    두 Class 사이에 일반 격리 규정이 없으면(SEGREGATE 관계 없음, 표상 "X")
    결과에서 자연스럽게 빠진다.
    """
    if not adjacent_chem_ids:
        return []

    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(
                _CYPHER_FIND_IMDG_CONFLICTS,
                target=target_chem_id,
                adjacent=adjacent_chem_ids,
            )
            return [record.data() async for record in result]

        return await session.execute_read(_tx)
