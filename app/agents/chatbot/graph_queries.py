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

# 벌크 액체화학물질 호환성그룹 참고축(data-pipeline/loaders/bulk_compatibility_neo4j_loader.py
# 가 적재) — INCOMPATIBLE_WITH/IS_CLASSIFIED_AS(MSDS 텍스트 마이닝)와는 근거가
# 다른 별도 신호라 같은 카테고리로 섞지 않고, service.py에서 category 라벨로
# 출처를 구분해 합친다. 그룹 경유(일반 규칙)와 화물쌍 직접 예외
# (BULK_COMPAT_BLOCKED_EXCEPTION) 두 가지를 모두 조회해야 한다 — 예외는
# 그룹 관계로는 안 잡히는 조합이기 때문(bulk_compatibility.py 참고).
_CYPHER_BULK_GROUP_INCOMPATIBLE = """
MATCH (c:Chemical {id: $chem_id})-[:IN_COMPATIBILITY_GROUP]->(g:CompatibilityGroup)
MATCH (g)-[:INCOMPATIBLE_WITH_GROUP]->(og:CompatibilityGroup)
MATCH (o:Chemical)-[:IN_COMPATIBILITY_GROUP]->(og)
WHERE o.id <> $chem_id
WITH g.group_no AS my_group, g.name AS my_group_name,
     og.group_no AS other_group, og.name AS other_group_name,
     collect(DISTINCT o) AS others
RETURN my_group, my_group_name, other_group, other_group_name,
       [o IN others | {chem_id: o.id, name_ko: o.name_ko, name_en: o.name_en, cas_no: o.cas_no}]
         AS chemicals
"""

_CYPHER_BULK_BLOCKED_EXCEPTION = """
MATCH (c:Chemical {id: $chem_id})-[:BULK_COMPAT_BLOCKED_EXCEPTION]->(o:Chemical)
RETURN o.id AS chem_id, o.name_ko AS name_ko, o.name_en AS name_en, o.cas_no AS cas_no
"""

# IMDG 공인 일반 격리표(imdg_segregation_loader.py) 기반 — INCOMPATIBLE_WITH/
# IS_CLASSIFIED_AS(MSDS 텍스트 마이닝)와는 별도 신호. safety 에이전트의 pairwise
# 판정(assess_safety, INCOMPATIBILITY_CHECK 의도가 위임하는 경로)에는 이미
# 반영돼 있었지만, 이 목록형 조회(INCOMPATIBLE_LIST 의도)에는 지금까지 빠져
# 있었다 — "이 물질과 안 맞는 게 뭐야" 질문이 IMDG 근거로만 걸리는 조합을
# 놓칠 수 있었다(2026-08-21 발견, bulk축 추가 때 지적받은 것과 같은 종류의
# 공백). "확정 규정 없음(X)"으로 판정 불가능한 조합은 여기 넣지 않는다 —
# 목록형 질문에 "아마 위험할 수도" 항목을 섞으면 확정 사실처럼 오독되기 쉽다.
_CYPHER_IMDG_SEGREGATION_LIST = """
MATCH (c:Chemical {id: $chem_id})-[:HAS_IMDG_CLASS]->(ca:ImdgClass)
MATCH (ca)-[s:SEGREGATE]->(cb:ImdgClass)
MATCH (o:Chemical)-[:HAS_IMDG_CLASS]->(cb)
WHERE o.id <> $chem_id
WITH ca.code AS my_class, cb.code AS other_class, s.code AS segregation_code,
     collect(DISTINCT o) AS others
RETURN my_class, other_class, segregation_code,
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


async def fetch_bulk_compatibility_groups(driver: AsyncDriver, chem_id: str) -> list[dict]:
    """벌크 호환성그룹 참고축 기준으로 대상 화물과 불호환인 화물 목록을
    그룹 단위로 묶어 반환한다. fetch_incompatible_groups와 같은 모양
    ([{"category": str, "chemicals": [...]}])이라 service.py에서 그대로
    IncompatibleCategoryGroup 리스트에 이어붙일 수 있다.

    category 라벨에 "(참고축)"을 명시해 MSDS/IMDG 근거와 구분한다 — 근거의
    성격이 다르다는 것을 프롬프트·화면 양쪽에서 알 수 있어야 한다.
    """
    group_rows = await _read(driver, _CYPHER_BULK_GROUP_INCOMPATIBLE, chem_id=chem_id)
    exception_rows = await _read(driver, _CYPHER_BULK_BLOCKED_EXCEPTION, chem_id=chem_id)

    groups: list[dict] = [
        {
            "category": (
                f"벌크호환성그룹(참고축) {row['my_group_name']}(그룹{row['my_group']}) "
                f"↔ {row['other_group_name']}(그룹{row['other_group']})"
            ),
            "chemicals": row["chemicals"],
        }
        for row in group_rows
    ]
    if exception_rows:
        groups.append({
            "category": "벌크호환성그룹(참고축) 개별 예외 규정 — 일반 그룹 규칙과 무관하게 강제 격리",
            "chemicals": [dict(row) for row in exception_rows],
        })
    return groups


async def fetch_imdg_segregation_groups(driver: AsyncDriver, chem_id: str) -> list[dict]:
    """IMDG 공인 일반 격리표 기준으로 대상 화물과 격리가 필요한 화물 목록을
    Class 조합 단위로 묶어 반환한다. fetch_incompatible_groups/
    fetch_bulk_compatibility_groups와 같은 모양([{"category","chemicals"}])."""
    rows = await _read(driver, _CYPHER_IMDG_SEGREGATION_LIST, chem_id=chem_id)
    return [
        {
            "category": (
                f"IMDG 공인 격리표 Class {row['my_class']}↔{row['other_class']} "
                f"(격리코드 {row['segregation_code']})"
            ),
            "chemicals": row["chemicals"],
        }
        for row in rows
    ]


async def fetch_all_chemicals(driver: AsyncDriver) -> list[dict]:
    return await _read(driver, _CYPHER_ALL_CHEMICALS)
