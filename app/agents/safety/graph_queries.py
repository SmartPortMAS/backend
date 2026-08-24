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


# ─────────────────────────────────────────────────────────────────────────────
# 화물 자신의 IMDG Class 단독 조회 — find_imdg_segregation_conflicts는 SEGREGATE
# 관계(=충돌)가 있을 때만 두 화물의 Class 값을 같이 돌려주는 구조라, 충돌이 없는
# 화물쌍은 각자 무슨 Class인지조차 호출부가 알 방법이 없었다. 그 결과 "SEGREGATE
# 관계 없음"이 "공인 규정상 X(격리 불필요)"인지 "이 Class 조합 자체가 그래프에
# 안 실림"인지 화면에서 구분이 안 됐다(로더가 9x9 전체가 아니라 실제 등재된
# 화물의 Class만 SEGREGATE 엣지로 적재하기 때문 — imdg_segregation_loader.py 참고).
# 이 조회는 그 구분을 위한 최소 정보(화물별 Class 자체)만 별도로 준다.
# ─────────────────────────────────────────────────────────────────────────────

_CYPHER_FIND_IMDG_CLASSES = """
MATCH (c:Chemical)-[:HAS_IMDG_CLASS]->(cls:ImdgClass)
WHERE c.id IN $chem_ids
RETURN c.id AS chem_id, cls.code AS class_code
"""


_CYPHER_FIND_IMDG_NO_SEGREGATION_REQUIRED = """
MATCH (a:Chemical {id: $target})-[:HAS_IMDG_CLASS]->(ca:ImdgClass)
MATCH (b:Chemical)-[:HAS_IMDG_CLASS]->(cb:ImdgClass)
WHERE b.id IN $adjacent
MATCH (ca)-[:NO_SEGREGATION_REQUIRED]->(cb)
RETURN DISTINCT b.id AS chem_id
"""


async def find_imdg_no_segregation_required(
    driver: AsyncDriver,
    *,
    target_chem_id: str,
    adjacent_chem_ids: list[str],
) -> set[str]:
    """공인 IMDG 격리표상 "X"(격리 불필요 — 모호함이 아니라 확정된 안전 답변)로
    확정된 인접 chem_id 집합. imdg_segregation_loader.py가 적재한
    NO_SEGREGATION_REQUIRED 관계를 조회한다.

    이게 필요한 이유: 두 화물의 Class가 둘 다 알려져 있는데 SEGREGATE 관계가
    없으면(compute_imdg_unconfirmed_floor 참고) "확정 X"인지 "그래프 커버리지
    밖"인지 예전엔 구분이 안 됐다 — 특히 같은 Class끼리는 공인 표상 예외 없이
    X인데(17개 Class 전부 확인됨), 이걸 "미확인"으로 잘못 다뤄 같은 Class
    화물끼리마다(예: 벤젠-가솔린, 둘 다 Class 3) 불필요한 주의 경고가 났었다
    (2026-08-21 발견). 이 조회 결과에 들어있으면 "확정 안전"이므로
    compute_imdg_unconfirmed_floor의 격상 대상에서 제외해야 한다.
    """
    if not adjacent_chem_ids:
        return set()

    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(
                _CYPHER_FIND_IMDG_NO_SEGREGATION_REQUIRED,
                target=target_chem_id,
                adjacent=adjacent_chem_ids,
            )
            return [record.data() async for record in result]

        rows = await session.execute_read(_tx)
    return {row["chem_id"] for row in rows}


async def find_imdg_classes(
    driver: AsyncDriver,
    *,
    chem_ids: list[str],
) -> dict[str, str]:
    """화물 id -> IMDG Class 코드. HAS_IMDG_CLASS 관계가 없는 화물은 결과에서 빠진다
    (그래프에 Class 자체가 안 실려 있다는 뜻 — 호출부가 '모름'으로 구분해야 한다)."""
    if not chem_ids:
        return {}

    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_FIND_IMDG_CLASSES, chem_ids=chem_ids)
            return [record.data() async for record in result]

        rows = await session.execute_read(_tx)
    return {row["chem_id"]: row["class_code"] for row in rows}


# ─────────────────────────────────────────────────────────────────────────────
# 벌크 액체화학물질 호환성그룹 참고축 (data-pipeline의
# bulk_compatibility_neo4j_loader.py가 적재한 그래프). find_incompatible_conflicts
# (MSDS 텍스트 마이닝)·find_imdg_segregation_conflicts(IMDG 공인 격리표)와는 근거가
# 다른 세 번째 신호 — 이 축은 처음엔 backend 안의 정적 Python dict로 구현했다가,
# 2026-08-21에 챗봇(GraphRAG)도 같은 정보를 봐야 한다는 게 확인돼 Neo4j로 이관했다.
# 판정 로직의 권위를 하나로 유지하려고 안전관제 에이전트도 이 그래프 조회로
# 전환했다 — Python dict는 더 이상 안전관제 판정에 쓰이지 않는다(참고자료 출처
# 설명은 bulk_compatibility_neo4j_loader.py 상단 주석 참고).
# ─────────────────────────────────────────────────────────────────────────────

_CYPHER_FIND_BULK_GROUP_CONFLICTS = """
MATCH (a:Chemical {id: $target})-[:IN_COMPATIBILITY_GROUP]->(ga:CompatibilityGroup)
MATCH (b:Chemical)-[:IN_COMPATIBILITY_GROUP]->(gb:CompatibilityGroup)
WHERE b.id IN $adjacent
MATCH (ga)-[:INCOMPATIBLE_WITH_GROUP]->(gb)
RETURN b.id AS chem_id, b.name_ko AS name_ko
"""

_CYPHER_FIND_BULK_GROUPS = """
MATCH (c:Chemical)-[:IN_COMPATIBILITY_GROUP]->(g:CompatibilityGroup)
WHERE c.id IN $chem_ids
RETURN c.id AS chem_id, g.group_no AS group_no, g.group_type AS group_type, g.name AS group_name
"""

_CYPHER_FIND_BULK_SAFE_EXCEPTIONS = """
MATCH (a:Chemical {id: $target})-[:BULK_COMPAT_SAFE_EXCEPTION]->(b:Chemical)
WHERE b.id IN $adjacent
RETURN b.id AS chem_id
"""

_CYPHER_FIND_BULK_BLOCKED_EXCEPTIONS = """
MATCH (a:Chemical {id: $target})-[:BULK_COMPAT_BLOCKED_EXCEPTION]->(b:Chemical)
WHERE b.id IN $adjacent
RETURN b.id AS chem_id
"""


async def find_bulk_group_conflicts(
    driver: AsyncDriver,
    *,
    target_chem_id: str,
    adjacent_chem_ids: list[str],
) -> list[dict]:
    """대상 화물과 인접 화물들의 벌크 호환성그룹 간 불호환 충돌을 탐색한다.

    두 화물의 그룹이 모두 확인됐는데 INCOMPATIBLE_WITH_GROUP 관계가 없으면
    (일반 충돌 규칙 밖) 결과에서 자연스럽게 빠진다 — 이 경우는 find_bulk_groups로
    "그룹은 알지만 충돌 규칙에는 안 걸림"과 "그룹 자체를 모름"을 구분해야 한다.
    """
    if not adjacent_chem_ids:
        return []

    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(
                _CYPHER_FIND_BULK_GROUP_CONFLICTS,
                target=target_chem_id,
                adjacent=adjacent_chem_ids,
            )
            return [record.data() async for record in result]

        return await session.execute_read(_tx)


async def find_bulk_groups(
    driver: AsyncDriver,
    *,
    chem_ids: list[str],
) -> dict[str, tuple[int, str, str]]:
    """화물 id -> (그룹번호, 그룹구분, 그룹명). IN_COMPATIBILITY_GROUP 관계가 없는
    화물(이 참고축 36종 커버리지 밖)은 결과에서 빠진다."""
    if not chem_ids:
        return {}

    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_FIND_BULK_GROUPS, chem_ids=chem_ids)
            return [record.data() async for record in result]

        rows = await session.execute_read(_tx)
    return {row["chem_id"]: (row["group_no"], row["group_type"], row["group_name"]) for row in rows}


async def find_bulk_exceptions(
    driver: AsyncDriver,
    *,
    target_chem_id: str,
    adjacent_chem_ids: list[str],
) -> tuple[set[str], set[str]]:
    """(안전 예외로 걸린 인접 chem_id 집합, 강제차단 예외로 걸린 인접 chem_id 집합)."""
    if not adjacent_chem_ids:
        return set(), set()

    async with driver.session() as session:

        async def _tx_safe(tx):
            result = await tx.run(
                _CYPHER_FIND_BULK_SAFE_EXCEPTIONS, target=target_chem_id, adjacent=adjacent_chem_ids
            )
            return [record.data() async for record in result]

        async def _tx_blocked(tx):
            result = await tx.run(
                _CYPHER_FIND_BULK_BLOCKED_EXCEPTIONS, target=target_chem_id, adjacent=adjacent_chem_ids
            )
            return [record.data() async for record in result]

        safe_rows = await session.execute_read(_tx_safe)
        blocked_rows = await session.execute_read(_tx_blocked)

    return (
        {row["chem_id"] for row in safe_rows},
        {row["chem_id"] for row in blocked_rows},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 판정 가능성(assessability) 조회 — 2026-08-23 추가
#
# 왜 필요한가: 지금까지 "혼재금지 충돌 0건"은 무조건 안전으로 다뤄졌는데, 그
# 0건에는 성격이 다른 두 가지가 섞여 있다.
#   ① 양쪽 근거를 다 보고 겹치는 게 없었다        → 진짜 안전
#   ② 애초에 볼 근거가 없어서 아무것도 못 걸렀다   → 판정 불가
#
# 실측(2026-08-23, 36종 전수):
#   · 12개 IncompatibleMaterial 카테고리 중 IS_CLASSIFIED_AS 멤버가 있는 것은
#     일부뿐이다. 멤버가 0인 카테고리(물/수분·열/점화원·중합반응물질 등)는
#     "화물"이 아니라 환경 조건이라, 아무리 많은 화물이 그것을 기피한다고 써도
#     화물 대 화물 충돌을 만들어낼 수 없다.
#   · 그 결과 36종 중 31종은 유효 기피 카테고리가 '산소/공기' 하나뿐이고,
#     거기 속한 화물은 수소 1종이다 — 즉 이 31종은 수소를 뺀 어떤 화물과도
#     구조적으로 충돌이 나올 수 없다.
#   · 근본 원인은 KOSHA MSDS의 J08("피해야 할 물질")이 36종 중 27종에서
#     "자료없음"이라는 데 있다(2026-08-23 KOSHA API 직접 호출로 원천 확인 —
#     우리 수집 문제가 아니라 원문에 값이 없다). 그 27종의 INCOMPATIBLE_WITH는
#     전부 J02·J06(안정성·조건)에서 나온 환경 조건이다.
#
# 그래서 "충돌 없음"을 안전으로 단정하지 않으려면 이 조회가 필요하다.
# 살아있는 카테고리 목록을 상수로 박지 않고 매번 그래프에서 계산하는 이유:
# 로더를 다시 돌려 IS_CLASSIFIED_AS가 늘면 판정 가능 범위도 자동으로 넓어져야
# 하는데, 하드코딩하면 데이터와 코드가 조용히 어긋난다.
# ─────────────────────────────────────────────────────────────────────────────

_CYPHER_LIVE_CATEGORIES = """
MATCH (m:IncompatibleMaterial)<-[:IS_CLASSIFIED_AS]-(:Chemical)
RETURN DISTINCT m.name AS name
"""

_CYPHER_ASSESSABILITY_FACTS = """
UNWIND $chem_ids AS cid
MATCH (c:Chemical {id: cid})
OPTIONAL MATCH (c)-[:INCOMPATIBLE_WITH]->(m:IncompatibleMaterial)
OPTIONAL MATCH (c)-[:IS_CLASSIFIED_AS]->(k:IncompatibleMaterial)
RETURN c.id AS chem_id,
       collect(DISTINCT m.name) AS avoids,
       collect(DISTINCT k.name) AS classified_as
"""


async def find_live_categories(driver: AsyncDriver) -> set[str]:
    """실제로 충돌을 만들어낼 수 있는 카테고리 이름 집합.

    IS_CLASSIFIED_AS 멤버가 하나도 없는 카테고리는 2-hop 경로의 반대편이 비어
    있어 충돌을 생성할 수 없다 — 그런 카테고리를 기피한다는 사실은 "화물 대 화물"
    판정에 아무 기여도 하지 못하므로 판정 가능성 계산에서 제외한다.
    """
    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_LIVE_CATEGORIES)
            return [record.data() async for record in result]

        rows = await session.execute_read(_tx)
    return {row["name"] for row in rows if row["name"]}


async def find_assessability_facts(
    driver: AsyncDriver, *, chem_ids: list[str]
) -> dict[str, tuple[set[str], set[str]]]:
    """chem_id -> (기피하는 카테고리 집합, 자신이 속한 카테고리 집합).

    그래프에 없는 chem_id는 결과에서 빠진다 — 호출부는 그 사실 자체를
    "판정 불가"로 다뤄야 한다(PostgreSQL에는 있지만 Neo4j 적재 이후 KOSHA에서
    lazy-fetch된 화물이 여기 해당한다).
    """
    if not chem_ids:
        return {}

    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_ASSESSABILITY_FACTS, chem_ids=chem_ids)
            return [record.data() async for record in result]

        rows = await session.execute_read(_tx)
    return {
        row["chem_id"]: (
            {c for c in row["avoids"] if c},
            {c for c in row["classified_as"] if c},
        )
        for row in rows
    }
