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
