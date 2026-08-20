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

# onsan_scope: 온산 MVP 대상 선석인지(berth_neo4j_loader.ONSAN_SCOPE_WHARF_NAMES).
# WHERE로 거르지 않고 값만 실어 보낸다 — 후보풀 제한은 하드 필터가 아니라
# service.py의 정렬 우선순위로 처리하기 때문이다. 하드로 거르면 온산에 적합
# 선석이 3개 미만인 화물(원유는 온산 부이 2기뿐)에서 후보가 줄거나 0이 된다.
# 속성이 없는 그래프(로더 재적재 전)에서는 NULL이 오므로 호출부가 falsy로 다룬다.
#
# 08_스케줄링_전면재설계_자동배정_설계문서.md §4.1.3-A is_eligible 3·4번 게이트를
# 여기 통합했다(2026-08-19):
#   게이트 3(일반 DWT 상한) — vessel/berth 어느 한쪽이라도 DWT를 모르면 막지 않는다
#     (VesselSpec.dwt_t는 선택 필드; berth.berth_capacity는 여전히 69개 중 다수가
#     결측 — berth_neo4j_loader.py가 ONSAN_BERTH_CAPACITY_DWT로 보정한 뒤에도
#     온산 밖 다수 선석은 결측으로 남는다).
#   게이트 4(부이 VLCC 전용) — b.length_m이 NULL이면 부이 계열(안벽이 없는 해상
#     계류점, 2026-08-19 staging 실측 확인). dwt_t가 NULL이면 이 게이트는
#     탈락한다(3번과 반대 방향 — "부이는 확실할 때만 배정"이라는 의도적 비대칭,
#     설계문서에 명시된 대로). 임계값은 이 파일 하단에 이미 정의된 VLCC_BUOY_DWT
#     (정박지 선택 로직과 동일 상수, 150_000)를 그대로 재사용한다 — 새로 만들지
#     않는다(모듈 로드 후 호출되므로 정의 순서와 무관하게 참조 가능).

_CYPHER_FIND_ELIGIBLE_BERTHS = """
MATCH (b:Berth)-[:HANDLES]->(:CargoCategory {name: $category})
WHERE b.depth_m IS NOT NULL AND b.depth_m >= $min_depth
  AND ($dwt_t IS NULL OR b.berth_capacity IS NULL OR $dwt_t <= b.berth_capacity)
  AND (b.length_m IS NOT NULL OR $dwt_t >= $vlcc_buoy_dwt_threshold)
RETURN b.id AS berth_id, b.wharf_name AS wharf_name, b.port_name AS port_name,
       b.depth_m AS depth_m, b.berth_group AS berth_group,
       b.length_m AS length_m, b.berth_capacity AS max_dwt,
       b.unload_capacity AS unload_capacity,
       b.latitude AS latitude, b.longitude AS longitude,
       coalesce(b.onsan_scope, false) AS onsan_scope
ORDER BY b.depth_m DESC
"""

# 온산 MVP(feature/onsan-mvp) 이식: 전용 선석이 점유 중일 때 같은 운영사/파이프라인
# 안에서 대체 가능한 선석을 찾는다(build_substitutability.py의 SUBSTITUTABLE_WITH
# 관계). ADJACENT_TO(물리적 인접 = 혼재위험)와는 완전히 별도 관계다.
_CYPHER_FIND_SUBSTITUTABLE_BERTHS = """
MATCH (b:Berth {id: $berth_id})-[r:SUBSTITUTABLE_WITH]->(target:Berth)
RETURN target.id AS berth_id, target.wharf_name AS wharf_name, target.port_name AS port_name,
       target.depth_m AS depth_m, target.berth_group AS berth_group,
       target.latitude AS latitude, target.longitude AS longitude,
       coalesce(target.onsan_scope, false) AS onsan_scope,
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

_CYPHER_GET_BERTH_CATEGORIES = """
MATCH (b:Berth {id: $berth_id})-[:HANDLES]->(cat:CargoCategory)
RETURN collect(cat.name) AS categories
"""

_CYPHER_GET_BERTH_BY_WHARF_NAME = """
MATCH (b:Berth {wharf_name: $wharf_name})
RETURN b.id AS berth_id, b.wharf_name AS wharf_name, b.port_name AS port_name,
       b.depth_m AS depth_m, b.berth_group AS berth_group,
       coalesce(b.onsan_scope, false) AS onsan_scope
LIMIT 1
"""

_CYPHER_FIND_ADJACENT_CATEGORIES = """
MATCH (b:Berth)-[r:ADJACENT_TO]->(n:Berth)-[:HANDLES]->(cat:CargoCategory)
WHERE b.id IN $berth_ids
RETURN b.id AS berth_id, n.id AS adjacent_berth_id, n.wharf_name AS adjacent_wharf_name,
       r.distance_m AS distance_m, collect(DISTINCT cat.name) AS categories
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
    dwt_t: float | None = None,
) -> list[dict]:
    """화물 카테고리를 취급하고 수심 조건(min_depth 이상)을 만족하는 선석 목록.

    depth_m이 NULL인 선석은 안전 판단이 불가능하므로 결과에서 제외한다
    (모르면 추천하지 않는다).

    dwt_t(선박 DWT, 선택)가 주어지면 일반 DWT 상한 게이트와 부이(VLCC 전용)
    게이트를 함께 적용한다(08_스케줄링_전면재설계_자동배정_설계문서.md
    §4.1.3-A) — 두 게이트의 "모르면 어떻게 하는가"가 서로 반대이므로 Cypher
    쿼리 주석을 참고할 것.
    """
    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(
                _CYPHER_FIND_ELIGIBLE_BERTHS,
                category=category,
                min_depth=min_depth,
                dwt_t=dwt_t,
                vlcc_buoy_dwt_threshold=VLCC_BUOY_DWT,
            )
            return [record.data() async for record in result]

        return await session.execute_read(_tx)


async def get_berth_categories(driver: AsyncDriver, *, berth_id: str) -> list[str]:
    """이 선석이 HANDLES로 취급하는 카테고리 목록(§4.1.2, 3-tier: 원유/유류/액체화학 등).

    anchorage_promoter(§5.4)가 정박지 대기열을 이 선석 카테고리로 먼저 걸러내는 데
    쓴다 — build_candidate_for_wharf_name(검증모드)은 화물 카테고리를 확인하지
    않으므로(주석 참고), 카테고리 부적합한 화물이 검증모드로 잘못 통과하지 않게
    호출부에서 먼저 걸러야 한다.
    """
    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_GET_BERTH_CATEGORIES, berth_id=berth_id)
            record = await result.single()
            return record["categories"] if record else []

        return await session.execute_read(_tx)


async def get_berth_by_wharf_name(driver: AsyncDriver, *, wharf_name: str) -> dict | None:
    """Berth.wharf_name 정확히 일치하는 선석 하나를 조회한다(검증모드: 사전배정 선석 확인용).

    find_eligible_berths처럼 카테고리/수심으로 거르지 않는다 — 이미 정해진 선석
    하나가 맞는지만 보는 용도라, 없으면 그대로 None(호출부가 "선석을 찾을 수
    없음"으로 처리한다).
    """
    async with driver.session() as session:

        async def _tx(tx):
            result = await tx.run(_CYPHER_GET_BERTH_BY_WHARF_NAME, wharf_name=wharf_name)
            record = await result.single()
            return record.data() if record else None

        return await session.execute_read(_tx)


async def find_adjacent_categories(
    driver: AsyncDriver,
    *,
    berth_ids: list[str],
) -> dict[str, list[dict]]:
    """각 후보 선석의 인접 선석과, 그 인접 선석이 취급하는 카테고리 목록.

    adjacent_wharf_name은 mart.berth_current_cargo(실제 재항 화물)를 조회하는 키로
    쓰인다(service.py `_real_adjacent_cargo_by_wharf`) — categories는 그게 없을 때의
    폴백 근사치일 뿐이다.

    distance_m은 좌표 계산으로 구해진 쌍만 값이 있고(PILOT_ADJACENT_PAIRS 수동
    큐레이션 쌍은 None) — safety/rule_engine.py가 IMDG 격리코드별 거리 임계값
    판정에 쓴다(2026-08-16). None이면 "인접은 확인됐지만 정확한 거리는 모름"
    이라는 뜻이라, 호출부가 보수적으로(임계값 통과로 보지 않고) 다뤄야 한다.

    Returns:
        { berth_id: [{"adjacent_berth_id": ..., "adjacent_wharf_name": ...,
                       "distance_m": ..., "categories": [...]}, ...] }
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
            {
                "adjacent_berth_id": row["adjacent_berth_id"],
                "adjacent_wharf_name": row["adjacent_wharf_name"],
                "distance_m": row["distance_m"],
                "categories": row["categories"],
            }
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
