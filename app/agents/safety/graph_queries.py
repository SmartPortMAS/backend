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
# ─────────────────────────────────────────────────────────────────────────────
# 실측(2026-09-16, 151종 전수 22,650 순서쌍 — 현재 유효)
#   · **판정불가가 15,510쌍(68.5%)**이다. 이 조회가 없으면 그 전부가 "안전"으로
#     잘못 집계된다. 판정불가 비율이 높다는 것은 로직 결함이 아니라 근거 데이터의
#     커버리지 공백이며, 그 사실 자체를 드러내는 것이 이 조회의 목적이다.
#   · 미보유 현황: IS_CLASSIFIED_AS 57종 / 벌크 호환성그룹 30종
#     (전자는 KOSHA 원문 결측, 후자는 46 CFR 150 Table 1에 CAS 미등재)
#   · 화물이 36 -> 151종으로 늘면서 MSDS 축은 되살아났다 — 전체 쌍의 37.4%에서
#     충돌을 잡는다(아래 과거 기록의 "31종은 수소 외 충돌 불가" 상태에서 벗어남).
#
# 과거 기록(2026-08-23, 36종 전수 — 더 이상 유효하지 않음):
#   · 12개 IncompatibleMaterial 카테고리 중 IS_CLASSIFIED_AS 멤버가 있는 것은
#     일부뿐이었다. 멤버가 0인 카테고리(물/수분·열/점화원·중합반응물질 등)는
#     "화물"이 아니라 환경 조건이라, 아무리 많은 화물이 그것을 기피한다고 써도
#     화물 대 화물 충돌을 만들어낼 수 없다. ★ 이 지적은 지금도 유효하다 —
#     환경 조건 카테고리는 기상 축과 연결하지 않는 한 영원히 발동하지 않는다.
#   · 당시 36종 중 31종은 유효 기피 카테고리가 '산소/공기' 하나뿐이었고,
#     거기 속한 화물은 수소 1종이었다.
#   · 근본 원인은 KOSHA MSDS의 J08("피해야 할 물질")이 36종 중 27종에서
#     "자료없음"이라는 데 있다(2026-08-23 KOSHA API 직접 호출로 원천 확인 —
#     우리 수집 문제가 아니라 원문에 값이 없다). 이 원천 결측은 151종에서도
#     그대로여서, 현재 IS_CLASSIFIED_AS 미보유 57종의 주된 이유다.
# ─────────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# D3 — 인접 선석 확장 탐색 (9/17 회의 §3, 목표일 9/23)
#
# ★ 왜 새로 필요한가 (2026-09-22 실측)
#   위의 쿼리들은 전부 "대상 화물 1 : 이미 골라 준 화물 N" 1홉이다. 그 N을 누가
#   고르느냐가 문제였다 — berth_alerts.py 는 `mart.berth_current_cargo` 를
#   facility_name 으로 묶어 **같은 선석** 화물끼리만 짝지었다. 그래서 Neo4j 에
#   적재돼 있는 (:Berth)-[:ADJACENT_TO]->(:Berth) 120개 관계를 백엔드가 단 한
#   번도 타지 않았다(`grep -rn ADJACENT_TO backend/app` → 0건).
#
#   결과적으로 "옆 부두에 상극 화물이 붙어 있다"는 조합은 지금까지 아무 경고도
#   내지 않았다. 실측 인접쌍은 부두 단위 11쌍 — S-Oil 1·2·4부두, 효성-달포,
#   OTK1-대한유화, 정일1-2, SK5~8 이 서로 251~484m 안에 있다.
#
# ★ 왜 "한 방"인가
#   재항 현황은 Postgres 에만 있는 실시간 상태라 그래프가 들고 있을 수 없다.
#   대신 호출부가 **이미 읽어 둔** 선석별 화물 목록을 그대로 파라미터로 넘긴다
#   (berth_alerts 는 전 선석 화물을 한 번에 읽으므로 추가 조회가 0이다).
#   그러면 선석→인접→재항화물→충돌이 Cypher 한 번의 탐색으로 끝난다.
#
# ★ 경로 문장을 Cypher 가 만든다
#   근거를 화면이 다시 조립하면 표현이 갈라진다. 어떤 관계를 타고 결론에
#   닿았는지는 그래프가 제일 정확히 알고 있으므로 여기서 문장을 만든다.
#
# ★ 같은 부두 안의 쌍은 일부러 뺀다(WHERE nb.wharf_name <> $wharf_name).
#   그건 berth_alerts 의 기존 같은-선석 판정이 이미 보고 있다 — 두 경로가 같은
#   쌍을 각각 경고하면 관제사가 같은 위험을 두 번 본다.
#   Berth 노드는 선석마다 1개라 한 부두에 여러 개다. 부두 단위로 접은 뒤
#   가장 가까운 거리만 남긴다(min) — 같은 부두쌍이 여러 줄로 불어나지 않게.
# ─────────────────────────────────────────────────────────────────────────────

_CYPHER_ADJACENT_BERTH_CONFLICTS = """
MATCH (here:Berth {wharf_name: $wharf_name})-[adj:ADJACENT_TO]->(nb:Berth)
WHERE nb.wharf_name <> $wharf_name
WITH nb.wharf_name AS neighbor_wharf, min(adj.distance_m) AS distance_m
UNWIND $neighbor_cargo AS nc
WITH neighbor_wharf, distance_m, nc
WHERE nc.wharf_name = neighbor_wharf
MATCH (a:Chemical {id: $target})
MATCH (b:Chemical {id: nc.chem_id})
// 충돌 근거 세 갈래를 한 서브쿼리로 합친다. 근거가 다른 신호라 어느 하나로
// 대체할 수 없다 — MSDS 텍스트 마이닝 2방향(비대칭이라 양쪽을 다 봐야 한다.
// 파일 상단 설명 참고) + IMDG Chapter 7.2 공인 격리표.
// `CALL (a, b) {` 는 Neo4j 5.23+ 의 변수 스코프 문법이다(현 서버 5.26).
// 예전 `CALL { WITH a, b` 는 5.26 에서 deprecated 경고를 낸다.
CALL (a, b) {
    MATCH (a)-[:INCOMPATIBLE_WITH]->(m)<-[:IS_CLASSIFIED_AS]-(b)
    RETURN 'MSDS_INCOMPATIBLE' AS basis, m.name AS via, NULL AS code,
           'target_incompatible_with_adjacent' AS direction
  UNION
    MATCH (b)-[:INCOMPATIBLE_WITH]->(m)<-[:IS_CLASSIFIED_AS]-(a)
    RETURN 'MSDS_INCOMPATIBLE' AS basis, m.name AS via, NULL AS code,
           'adjacent_incompatible_with_target' AS direction
  UNION
    MATCH (a)-[:HAS_IMDG_CLASS]->(ca:ImdgClass)-[s:SEGREGATE]->(cb:ImdgClass)
          <-[:HAS_IMDG_CLASS]-(b)
    RETURN 'IMDG_SEGREGATION' AS basis, ca.code + ' / ' + cb.code AS via,
           s.code AS code, 'imdg_segregation' AS direction
}
RETURN DISTINCT
    neighbor_wharf                       AS neighbor_wharf,
    distance_m                           AS distance_m,
    b.id                                 AS chem_id,
    coalesce(b.name_ko, nc.cargo_name)   AS name_ko,
    nc.callsgn                           AS callsgn,
    basis                                AS basis,
    via                                  AS via,
    code                                 AS segregation_code,
    direction                            AS direction,
    $wharf_name + ' —인접(' +
        CASE WHEN distance_m IS NULL THEN '거리 미상'
             ELSE toString(toInteger(round(distance_m))) + 'm' END +
        ')→ ' + neighbor_wharf +
        ' —재항→ ' + coalesce(b.name_ko, nc.cargo_name) +
        ' —' + CASE basis
                   WHEN 'MSDS_INCOMPATIBLE' THEN '혼재금지(' + via + ')'
                   ELSE 'IMDG격리(' + via + ' → ' + coalesce(code, '?') + ')'
               END +
        '→ ' + coalesce(a.name_ko, $target)  AS path_text
// 거리 미상(SK5~8 처럼 좌표 없이 부두번호로 이은 쌍)은 맨 뒤로. Cypher 는
// ORDER BY ... NULLS LAST 를 안 받아서 정렬 키를 직접 만든다.
ORDER BY coalesce(distance_m, 1000000.0), neighbor_wharf, chem_id
"""


async def find_adjacent_berth_conflicts(
    driver: AsyncDriver,
    *,
    wharf_name: str,
    target_chem_id: str,
    neighbor_cargo: list[dict],
) -> list[dict]:
    """대상 화물 하나에 대해 **인접 부두** 재항 화물과의 충돌을 한 번에 찾는다.

    neighbor_cargo 는 호출부가 이미 읽어 둔 재항 현황 그대로 넘긴다 —
    각 항목은 최소한 {"wharf_name", "chem_id"} 를 갖고, "callsgn"·"cargo_name"
    이 있으면 경로 문장에 함께 실린다.

    반환 행에는 `path_text`(그래프가 만든 경로 문장)가 들어 있다. 화면·프롬프트는
    이 문장을 그대로 쓰면 되고, 근거를 다시 조립하지 않는다.
    """
    if not wharf_name or not target_chem_id or not neighbor_cargo:
        return []

    payload = [
        {
            "wharf_name": c.get("wharf_name"),
            "chem_id": c.get("chem_id"),
            "callsgn": c.get("callsgn"),
            "cargo_name": c.get("cargo_name"),
        }
        for c in neighbor_cargo
        if c.get("wharf_name") and c.get("chem_id")
    ]
    if not payload:
        return []

    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(
                _CYPHER_ADJACENT_BERTH_CONFLICTS,
                wharf_name=wharf_name,
                target=target_chem_id,
                neighbor_cargo=payload,
            )
            return [record.data() async for record in result]

        return await session.execute_read(_tx)
