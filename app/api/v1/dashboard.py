"""관제사용 종합 모니터링 대시보드 — 읽기 전용 집계 API.

기존 4개 에이전트(weather/safety/scheduling/orchestrator)는 전부 "판정 요청"을
위한 POST API다. 이 라우터는 그 판정에 쓰이는 원본 데이터(실시간 관측, 선석/
정박지 현황, 선박 위치)를 대시보드 화면에 그대로 뿌려주기 위한 GET 전용
집계 엔드포인트라 새 판단 로직은 없다 — 전부 이미 있는 테이블/그래프를
한 화면 분량으로 모아 보여주기만 한다.

upa_* 테이블(포트 호출/선박위치)은 data-pipeline UPA 로더가 auto_create로
만든 테이블이라(scheduling/occupancy.py와 동일 원칙) backend Alembic이 소유하지
않는다 — ORM 모델 없이 raw SQL로 읽기만 한다.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.berth_alerts import build_berth_alerts
from app.agents.safety.safety_index import build_safety_index
from app.core.deps import get_session
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# ─────────────────────────────────────────────────────────────────────────────
# 무거운 전수 판정 응답의 TTL 캐시
#
# /alerts(재항 전수 혼재·격리·흘수 판정)와 /safety-index(6축 계산)는 요청마다
# 3초 안팎이 걸린다(2026-08-22 실측 3.2s / 2.6s — 나머지 GET 은 수십 ms).
# 대시보드가 폴링마다 /alerts 를 부르므로 이 둘이 체감 성능의 전부였다.
#
# 판정의 원천(재항 현황·화물·기상)은 cloud_pull 이 시간 단위로 갱신하므로,
# 5분 캐시는 의미를 잃지 않는다 — 같은 입력을 5분에 한 번만 다시 계산할 뿐이다.
# 프로세스 메모리 캐시라 재시작하면 비워지고, 워커 1개(uvicorn 기본) 전제다.
_TTL_CACHE: dict = {}
_TTL_SECONDS = 300.0


async def _cached(key: str, producer):
    import time as _time
    now = _time.monotonic()
    hit = _TTL_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _TTL_SECONDS:
        return hit[1]
    value = await producer()
    _TTL_CACHE[key] = (now, value)
    return value


# --------------------------------------------------------------------------
# 기상/조위/파고 현황 — mart.weather_now (기상·조위·파고 최신 관측 1행을 이미
# LEFT JOIN 으로 결합해 둔 뷰). 세 테이블을 각각 조회하던 것을 한 번의 조회로
# 대체한다 — 판정 로직(정상/주의/경보)은 여전히 /api/v1/weather/assess 소관이고,
# 여기서는 그 판정에 쓰이는 원본 관측치만 그대로 보여준다.
# --------------------------------------------------------------------------

_OBS_MAX_AGE_HOURS = 3  # weather 에이전트의 MAX_STALENESS와 동일 기준

_QUERY_WEATHER_NOW = text("SELECT * FROM mart.weather_now")


def _factor(observed_at: datetime | None, value, unit: str, as_of: datetime) -> dict:
    is_stale = observed_at is None or (as_of - observed_at).total_seconds() > _OBS_MAX_AGE_HOURS * 3600
    return {"value": value, "unit": unit, "observed_at_utc": observed_at, "is_stale": is_stale}


@router.get("/weather", summary="기상·조위·파고 현황 조회")
async def get_weather_overview(db: AsyncSession = Depends(get_session)) -> dict:
    """풍속/파고/조위 최신 관측치 한 화면분(참고용 — 판정은 /api/v1/weather/assess에서)."""
    as_of = datetime.now(timezone.utc)
    row = (await db.execute(_QUERY_WEATHER_NOW)).mappings().first()
    if row is None:
        row = {}

    return {
        "wind": _factor(row.get("weather_observed_at_utc"), row.get("wind_speed_ms"), "m/s", as_of),
        "wave": _factor(row.get("wave_observed_at_utc"), row.get("wave_height_sig_m"), "m", as_of),
        "tide": _factor(row.get("tide_observed_at_utc"), row.get("tide_level_cm"), "cm", as_of),
        "air_temp_c": row.get("air_temp_c"),
        "humidity_pct": row.get("humidity_pct"),
        "visibility_m": row.get("visibility_m"),
        # ↓ mart.weather_now 신규 노출 컬럼 (기존 3-쿼리 버전엔 없던 값)
        "wind_dir_deg": row.get("wind_dir_deg"),
        "gust_ms": row.get("gust_ms"),
        "current_speed_cms": row.get("current_speed_cms"),
        "current_dir_deg": row.get("current_dir_deg"),
    }


# --------------------------------------------------------------------------
# 선석 현황 (Neo4j Berth + mart.vessel_presence 점유 현황)
#
# ★ 2026-09-17 — 점유는 UPA 선박위치로 판정한다 (mart_views.sql 2-1절).
# 예전엔 upa_port_call 에서 "입항했고 출항 기록 없음"인 입항 건을 셌는데, 출항
# 처리가 빠진 유령 기록이 남아 선석 58곳에 818척(그중 775척은 입항 7일 초과)이
# 점유로 잡혔고, 입항~출항 구간에 든 정박지 대기 배도 선석 점유로 세졌다.
# 이제 "최신 위치가 들어오고, 멈춰 있고, 선석에 붙어 있는 배"만 센다 — 한 척은
# 한 곳에만 있으므로 중복도 생기지 않는다. port_call 은 뷰 안에서 "어느 선석으로
# 신고했나"라는 이름표로만 쓰인다.
#
# 아래 facility_alias 설명은 그 이름표를 마스터 표기로 바꿀 때 여전히 유효하다
# (뷰 안에서 거친다). VTS 운항관제
# (upa_port_call.facility_name)와 선석 제원 마스터(upa_berth_facility.wharf_name)는
# 서로 다른 표기 체계라 예전처럼 문자열 완전일치로 붙이면 점유 중인 선석
# 대부분이 "여유"로 오표시된다(실측: 92% 오표시).
#
# facility_alias.wharf_name은 upa_berth_facility(정본)에서 그대로 온 값이고,
# Neo4j Berth.wharf_name도 같은 테이블에서 가공 없이 로드된다(berth_neo4j_loader.py
# 주석: "wharf_name은 upa_berth_facility_stg.csv 실제 값과 정확히 일치해야 한다")
# — 즉 둘은 문자 그대로 같은 값이다. 그래서 여기서는 매 요청마다 Neo4j에서 가져온
# wharf_name 목록을 Postgres로 다시 넘겨 정규화할 필요가 없다. facility_alias.wharf_name
# 기준으로 그냥 GROUP BY 하면 끝난다.
#
# facility_alias.facility_type != 'BERTH'인 행(OTHER·UNMAPPED·ANCHORAGE)은
# 이 조인에서 자연히 빠진다 — 정박지·호안 점유를 선석 점유로 잘못 세지 않는다.
# --------------------------------------------------------------------------

_CYPHER_ALL_BERTHS = """
MATCH (b:Berth)
OPTIONAL MATCH (b)-[:HANDLES]->(cat:CargoCategory)
RETURN b.id AS berth_id, b.wharf_name AS wharf_name, b.port_name AS port_name,
       b.port_operator_name AS operator, b.depth_m AS depth_m, b.berth_group AS berth_group,
       b.latitude AS latitude, b.longitude AS longitude,
       collect(DISTINCT cat.name) AS categories
"""

_QUERY_BERTH_OCCUPANCY = text("""
    SELECT berth_name AS wharf_name,
           count(*) AS occupant_count,
           array_agg(vessel_name ORDER BY vessel_name) AS vessel_names,
           array_agg(callsgn ORDER BY vessel_name) AS callsigns,
           array_agg(berth_basis ORDER BY vessel_name) AS bases,
           array_agg(berth_dist_m ORDER BY vessel_name) AS distances_m,
           max(snapshot_at_utc) AS observed_at_utc
    FROM mart.vessel_presence
    WHERE presence_zone = 'BERTH'
    GROUP BY berth_name
""")


@router.get("/berths", summary="선석 목록 및 실시간 점유 현황")
async def get_berths_overview(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """전체 선석 목록 + 취급화물 + 실시간 점유 현황(UPA 선박위치, mart.vessel_presence)."""
    async with neo4j_client.driver.session() as session:
        async def _tx(tx):
            result = await tx.run(_CYPHER_ALL_BERTHS)
            return [record.data() async for record in result]

        berths = await session.execute_read(_tx)

    occupancy_rows = (await db.execute(_QUERY_BERTH_OCCUPANCY)).mappings().all()
    # 같은 wharf_name을 가진 Neo4j Berth 노드가 여럿일 수 있다(예: 'SK2부두(민유)'와
    # 'SK2부두(국유)' — 마스터에 선석번호 구분이 없는 채로 두 노드가 있는 경우).
    # 그 경우 같은 점유 현황을 양쪽 다 보여준다 — 어느 쪽인지 구분할 근거가 없는
    # 걸 숨기지 않는다(P1 한계로 남겨둠).
    occupancy_by_wharf = {row["wharf_name"]: row for row in occupancy_rows}

    out = []
    for b in berths:
        occ = occupancy_by_wharf.get(b["wharf_name"])
        occupied = bool(occ and occ["occupant_count"])
        out.append({
            **b,
            "occupancy_status": "점유" if occupied else "여유",
            "current_vessel_names": occ["vessel_names"] if occupied else [],
            # 판정 근거 — '신고+위치'(신고 선석 1km 안) / '위치'(가장 가까운 선석 300m 안)
            # / '신고'(좌표 없는 부이 등). 화면이 "왜 점유로 봤나"를 그대로 보여줄 수 있게.
            "current_vessels": [
                {"vessel_name": n, "callsgn": c, "basis": s, "distance_m": d}
                for n, c, s, d in zip(
                    occ["vessel_names"], occ["callsigns"], occ["bases"], occ["distances_m"]
                )
            ] if occupied else [],
            "occupancy_observed_at_utc": occ["observed_at_utc"] if occupied else None,
        })
    return out


@router.get("/berths/unmapped", summary="선석·정박지로 판정되지 않은 정지 선박의 신고 시설 현황")
async def get_unmapped_facility_calls(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """지금 멈춰 있지만 위치로 선석·정박지에 붙지 않은 배를 최신 VTS 신고 시설별로 센다.

    대부분 호안·물양장·의장안벽(OTHER)과 '정박지 01'~'07'·'현대오일터미널신항부두'
    (UNMAPPED)다. 위치로 선석·정박지가 확인된 배는 여기서 빠진다 — 신고 표기가
    UNMAPPED 여도 좌표로 풀렸으면 더는 갭이 아니다(mart_views.sql 2-1절).
    """
    rows = (
        await db.execute(
            text("""
                SELECT vts_facility_name AS facility_name,
                       vts_facility_type AS facility_type,
                       count(*) AS occupant_count
                FROM mart.vessel_presence
                WHERE presence_zone = 'STOPPED'
                  AND vts_facility_type IN ('UNMAPPED', 'OTHER')
                  AND vts_event IS DISTINCT FROM '출항'
                GROUP BY vts_facility_name, vts_facility_type
                ORDER BY occupant_count DESC
            """),
        )
    ).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 정박지 현황 (Neo4j Anchorage + mart.vessel_presence 대기 현황)
#
# 2026-09-17 — 정박지 대기도 UPA 선박위치로 센다. 예전엔 upa_port_call 의
# '정박지-E1'~'E3' 신고만 facility_alias 로 붙여서 E1~E3 외 17곳(M1~M7·T·W·급유
# 정박지)은 늘 null 이었고, 유령 기록(정박지 "미출항" 141건 중 130건)이 대기
# 척수에 섞였다. 이제 upa_anchorage 구역(Neo4j Anchorage.id 와 같은 이름) 안에
# 멈춰 있는 배를 세므로 20곳 모두 값이 있다.
# --------------------------------------------------------------------------

_CYPHER_ALL_ANCHORAGES = """
MATCH (a:Anchorage)
RETURN a.id AS anchorage_id, a.name AS name, a.tonnage_rule AS tonnage_rule,
       a.anchorage_type AS anchorage_type, a.latitude AS latitude, a.longitude AS longitude
"""

_QUERY_ANCHORAGE_OCCUPANCY = text("""
    SELECT anchorage_name, count(*) AS occupant_count,
           array_agg(vessel_name ORDER BY vessel_name) AS vessel_names
    FROM mart.vessel_presence
    WHERE presence_zone = 'ANCHORAGE'
    GROUP BY anchorage_name
""")

# 위치 스냅샷이 아예 없으면(수집 전·DB 비어 있음) 0척이 아니라 "모름"이다.
_QUERY_POSITION_SNAPSHOT = text("SELECT max(snapshot_at_utc) AS at FROM mart.vessel_presence")


@router.get("/anchorages", summary="정박지 목록 및 실시간 대기 현황")
async def get_anchorages_overview(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """전체 정박지 목록 + 실시간 대기 척수(UPA 선박위치로 구역 안에 멈춘 배, mart.vessel_presence)."""
    async with neo4j_client.driver.session() as session:
        async def _tx(tx):
            result = await tx.run(_CYPHER_ALL_ANCHORAGES)
            return [record.data() async for record in result]

        anchorages = await session.execute_read(_tx)

    occupancy_rows = (await db.execute(_QUERY_ANCHORAGE_OCCUPANCY)).mappings().all()
    by_anchorage = {row["anchorage_name"]: row for row in occupancy_rows}
    snapshot_at = (await db.execute(_QUERY_POSITION_SNAPSHOT)).scalar()

    out = []
    for a in anchorages:
        occ = by_anchorage.get(a["anchorage_id"])
        out.append({
            **a,
            "current_occupants": (occ["occupant_count"] if occ else 0) if snapshot_at else None,
            "current_vessel_names": occ["vessel_names"] if occ else [],
            "occupancy_observed_at_utc": snapshot_at,
        })
    return out


# --------------------------------------------------------------------------
# 선박 위치 — mart.dashboard_current ("한 줄 조회" 뷰, 44개 컬럼).
#
# 기존엔 upa_vessel_position을 직접 긁어서 위치 필드만 내려줬다. 이 뷰로 바꾸면
# 같은 쿼리 한 방으로 화물선 여부(is_liquid_cargo_vessel — 프론트 mapVessel()의
# "위치 API에는 화물 정보가 없다" 주석이 가리키던 그 결측), 신호 신선도
# (presence_state/position_age_min), 서류상 재항 여부(doc_still_in_port),
# 식별 신뢰도(identity_confidence)까지 같이 온다 — mart 뷰가 원래 그러라고
# 만들어진 "진입점"이다(mart_views.sql 헤더 참고).
#
# SELECT * 로 전체 컬럼을 그대로 내려준다. facility_name/arrival_at_utc/
# departure_at_utc 등은 처음 연동 시점엔 upa_port_call 수집기가 429로 죽어
# 있어(2026-08-10 fix 이전) 전부 NULL이었는데, 수집 재개 후 실측으로 채워지는
# 걸 확인했다(port_call_overview 170행 중 75행) — 컬럼을 골라서 내려주면 이런
# "나중에 채워지는 값"이 뷰에는 있는데 API 응답에는 없는 상태로 조용히 방치될
# 수 있다. vessel_key로 이미 선박당 1행이 보장되므로(뷰 안에서 DISTINCT ON
# 처리 완료) 여기서 또 DISTINCT ON을 걸 필요가 없다.
# --------------------------------------------------------------------------

_QUERY_DASHBOARD_CURRENT_VESSELS = text("""
    SELECT *
    FROM mart.dashboard_current
    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    ORDER BY received_at_utc DESC NULLS LAST
""")


# 위치 판정 결과(mart.vessel_presence)를 선박마다 붙인다. 화면의 "접안 중 N척"·
# "정박지 대기 N척"이 AIS 자기신고 항해상태가 아니라 선석 점유(/berths)와 같은
# 기준으로 세어지게 하려는 것이다 — 한 화면에 기준이 두 개면 숫자가 서로 어긋난다
# (2026-09-17 실측: 자기신고 '정박(계류)' 34척 vs 위치 판정 선석 41척).
# SQL 에서 MMSI OR 호출부호로 조인하면 5초가 걸려 여기서 합친다.
_QUERY_PRESENCE_BY_VESSEL = text("""
    SELECT mmsi, callsgn, presence_zone, berth_name, berth_basis, anchorage_name
    FROM mart.vessel_presence
""")


@router.get("/vessels", summary="선박 실시간 위치 조회")
async def get_vessel_positions(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """선박별 최신 위치 1건(지도 표시용). 좌표 결측 행은 제외. 위치 판정(presence_*) 포함."""
    rows = (await db.execute(_QUERY_DASHBOARD_CURRENT_VESSELS)).mappings().all()
    presence = (await db.execute(_QUERY_PRESENCE_BY_VESSEL)).mappings().all()
    by_mmsi = {p["mmsi"]: p for p in presence if p["mmsi"] is not None}
    by_callsgn = {p["callsgn"]: p for p in presence if p["mmsi"] is None and p["callsgn"]}

    out = []
    for row in rows:
        item = dict(row)
        p = by_mmsi.get(item.get("mmsi")) if item.get("mmsi") is not None else by_callsgn.get(item.get("callsgn"))
        # 최신 스냅샷(3시간)에 없는 배는 판정이 없다 — null 로 두고 추정하지 않는다.
        item["presence_zone"] = p["presence_zone"] if p else None
        item["presence_berth_name"] = p["berth_name"] if p else None
        item["presence_berth_basis"] = p["berth_basis"] if p else None
        item["presence_anchorage_name"] = p["anchorage_name"] if p else None
        out.append(item)
    return out


# --------------------------------------------------------------------------
# 흘수·UKC 판정 — mart.berth_draught_check (조위 반영 가용수심 계산까지
# 뷰 안에서 끝나 있음). "이 배가 이 부두에 지금 붙어도 바닥에 안 닿나"를
# 판정 로직 없이 그대로 노출만 한다.
#
# 목록에 없는 부두(제원 미확보)나 흘수 미관측 선박은 draught_verdict='UNKNOWN'
# 으로 이미 뷰가 표기한다 — "모르면 안전으로 보지 않는다" 원칙이 뷰 단계에서
# 지켜지므로 여기서 UNKNOWN을 걸러내지 않는다(걸러내면 관제사가 "판정이 아예
# 없었던 선석"과 "OK로 판정된 선석"을 구분할 수 없게 된다).
# --------------------------------------------------------------------------

_QUERY_DRAUGHT_CHECK = text("""
    SELECT callsgn, facility_name, chart_depth_m, tide_level_m, available_depth_m,
           vessel_draught_m, ukc_m, ukc_required_m, draught_verdict,
           tide_observed_at_utc, draught_observed_at_utc, arrival_at_utc,
           chart_depth_max_m
    FROM mart.berth_draught_check
    ORDER BY CASE draught_verdict
                 WHEN 'NOT_ALLOWED' THEN 0
                 WHEN 'MARGINAL' THEN 1
                 WHEN 'CHECK' THEN 2      -- 선석별 수심 다름 → 접안 선석 확인 요청
                 WHEN 'UNKNOWN' THEN 3
                 ELSE 4
             END, arrival_at_utc DESC
""")


@router.get("/draught-check", summary="흘수·여유수심(UKC) 판정 조회")
async def get_draught_check(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """재항 중인 선박별 조위 반영 흘수·UKC 판정. 접안 불가(NOT_ALLOWED)가 먼저 오도록 정렬."""
    rows = (await db.execute(_QUERY_DRAUGHT_CHECK)).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 선석별 현재 취급 화물 — mart.berth_current_cargo.
#
# scheduling/category_map.py가 인접 선석 화물을 "카테고리 대표 1종"으로
# 근사하던 걸 실제 신고 화물로 대체하려고 만들어진 뷰(mart_views.sql 8번
# 참고)인데, 지금은 대시보드 표시 용도로만 노출한다 — category_map.py 교체는
# 별도 작업(안전 판정 입력이 걸린 변경이라 이 라우터 범위 밖).
#
# chem_id가 NULL인 행을 걸러내지 않는다: "위험물인데 정체 미확인"과 "위험물이
# 아님"은 다른 정보라서(뷰 자체 주석), 여기서 지우면 그 구분이 사라진다.
# --------------------------------------------------------------------------

_QUERY_BERTH_CURRENT_CARGO = text("""
    SELECT facility_name, callsgn, chem_id, cas_no, dg_un_no, cargo_name,
           imdg_class, packing_group, msds_matched, is_synthetic, cargo_basis,
           arrival_at_utc
    FROM mart.berth_current_cargo
    ORDER BY facility_name, arrival_at_utc DESC
""")


@router.get("/berth-cargo", summary="선석별 재항 위험물 화물 조회")
async def get_berth_current_cargo(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """재항 중인 선박이 신고한 위험물 화물, 선석별(인접 선석 혼재금지 판정 참고용)."""
    rows = (await db.execute(_QUERY_BERTH_CURRENT_CARGO)).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 수집기 생존 신호 — mart.pipeline_health.
#
# "지도에 배가 없다"와 "수집기가 죽어서 배가 안 보인다"는 다른 상황이다.
# 프론트는 pipeline_state <> 'OK' 이면 지도를 비우는 대신 배너를 띄워야
# 한다(뷰 자체 주석 참고). frontend/mock-server/dashboard_server.py가 이미
# 이 뷰로 "수집기 정지/원천 정지/정상" 3단계 배지를 구현해 뒀다 — 그 기준을
# backend 정식 엔드포인트로 옮긴 것뿐, 판정 로직은 전부 뷰 안에 있다.
# --------------------------------------------------------------------------

_QUERY_PIPELINE_HEALTH = text("SELECT * FROM mart.pipeline_health")


@router.get("/pipeline-health", summary="수집기·원천 데이터 신선도 확인")
async def get_pipeline_health(db: AsyncSession = Depends(get_session)) -> dict:
    """수집기·원천 데이터 신선도. pipeline_state: OK/COLLECTOR_DOWN/STALE_SOURCE/NO_DATA."""
    row = (await db.execute(_QUERY_PIPELINE_HEALTH)).mappings().first()
    if row is None:
        return {"pipeline_state": "NO_DATA", "diagnosis": "적재 데이터 없음"}
    return dict(row)


# --------------------------------------------------------------------------
# 최근 접안 이력 — 출항이 서류로 확정된(departure_at_utc 존재) 건만, 최신순.
#
# /berths·/anchorages는 "지금" 점유만 보여준다. 관제사가 "방금 전까지 뭐가
# 있었나"를 보려면(Gantt 패널 등) 별도로 완료된 접안 건을 조회해야 한다.
# 특정 권역(온산 등)으로 하드코딩해서 거르지 않는다 — facility_name 매칭
# 정확도가 아직 완벽하지 않은 상태(P1)에서 지역 필터까지 겹치면 어느 쪽
# 오차인지 구분이 안 된다. 대신 facility 부분일치 검색을 옵션으로 열어
# 소비자가 원하는 권역만 좁혀 볼 수 있게 한다.
# --------------------------------------------------------------------------

_QUERY_RECENT_HISTORY = text("""
    -- port_call_id가 같은 행이 원본에 중복 존재하는 경우가 있다(실측 확인,
    -- 재수집 UPSERT가 완전히 멱등하지 않은 것으로 보임 — 다른 집계 쿼리들도
    -- 전부 DISTINCT ON port_call_id로 방어하고 있어 같은 패턴을 따른다).
    -- DISTINCT ON은 그 자리에서 정렬 기준(port_call_id)이 고정되므로,
    -- "최신 departure 순 상위 N건"은 중복 제거 후 바깥에서 다시 정렬한다.
    WITH deduped AS (
        SELECT DISTINCT ON (port_call_id)
               COALESCE(NULLIF(btrim(vessel_name), ''), '(선명 미상)') AS vessel_name,
               callsgn, facility_name, arrival_at_utc, departure_at_utc
        FROM upa_port_call
        WHERE arrival_at_utc IS NOT NULL
          AND departure_at_utc IS NOT NULL
          AND departure_at_utc > arrival_at_utc
          AND (CAST(:facility_like AS text) IS NULL OR facility_name ILIKE CAST(:facility_like AS text))
        ORDER BY port_call_id, departure_at_utc DESC
    )
    SELECT * FROM deduped ORDER BY departure_at_utc DESC LIMIT :limit
""")


@router.get("/history", summary="최근 완료 접안 이력 조회")
async def get_recent_port_call_history(
    limit: int = 20,
    facility_like: str | None = None,
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    """최근 완료된 접안 이력(출항 확정 건). facility_like로 시설명 부분검색 가능(예: '%온산%')."""
    rows = (
        await db.execute(
            _QUERY_RECENT_HISTORY, {"limit": limit, "facility_like": facility_like}
        )
    ).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 선석별 재항 소요시간 통계 (mart.berth_dwell_stats)
#
# 스케줄링 에이전트는 지금까지 "점유인가 여유인가"만 답할 수 있었다. 관제사가
# 실제로 묻는 것은 "그럼 언제 비는가"인데, 출항 예정 시각(ETD)이 원천에 전혀
# 오지 않아(portmis_vessel.departure_sched_utc 779행 전부 NULL) 답할 수단이
# 없었다. 대신 실제 재항 이력 3만 건의 분포에서 추정한다.
#
# 화면은 이 값으로 점유 선석에 "약 N시간 후 해제 예상"을 붙인다. 추정임을
# 숨기지 않도록 표본 수와 P90 을 같이 내보낸다.
# --------------------------------------------------------------------------

_QUERY_BERTH_DWELL = text("""
    SELECT wharf_name, sample_count, median_hours, p90_hours
    FROM mart.berth_dwell_stats
    ORDER BY sample_count DESC
""")


@router.get("/berth-dwell", summary="선석별 재항 소요시간 통계 조회")
async def get_berth_dwell_stats(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """선석별 재항 소요시간(입항~출항 실측)의 중앙값·P90·표본 수.

    '하역 시간'이 아니라 '재항 시간'이다 — 접안 대기·검사·급유가 모두 포함돼
    있어 실제 작업시간보다 길다. 표본 5건 미만 선석은 뷰에서 이미 제외된다.
    """
    rows = (await db.execute(_QUERY_BERTH_DWELL)).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 수집 규모 통계 — 목서버(frontend/mock-server/dashboard_server.py)의
# stats.total_port_calls/liquid_callsgns 를 정식 엔드포인트로 대체.
#
# liquid_callsgns는 지도 AIS 레이어에서 액체화물선을 강조 표시하기 위한
# 호출부호 목록이다 — PORT-MIS 공식 선종코드(is_liquid_cargo_vessel) 기준.
# /vessels 응답에도 선박별로 같은 플래그가 있지만, 지도 레이어링 로직이
# "이 목록에 있으면 강조"처럼 집합 연산으로 짜여 있으면 이 형태가 더 쓰기
# 편하다(목서버가 실제로 이 형태로 썼다).
# --------------------------------------------------------------------------


@router.get("/stats", summary="수집 규모 통계 조회")
async def get_dashboard_stats(db: AsyncSession = Depends(get_session)) -> dict:
    """수집 규모 통계 + 액체화물선 호출부호 목록."""
    total_port_calls = await db.scalar(text("SELECT count(*) FROM upa_port_call"))
    liquid_callsgns = (
        await db.execute(
            text(
                "SELECT DISTINCT callsgn FROM portmis_vessel "
                "WHERE is_liquid_cargo_vessel AND callsgn IS NOT NULL"
            )
        )
    ).scalars().all()
    facility_type_rows = (
        await db.execute(
            text("""
                SELECT COALESCE(fa.facility_type, 'UNMAPPED') AS facility_type, count(*) AS n
                FROM upa_port_call pc
                LEFT JOIN mart.facility_alias fa ON fa.source_name = pc.facility_name
                GROUP BY 1
            """)
        )
    ).mappings().all()

    return {
        "total_port_calls": total_port_calls,
        # ↓ 목서버의 onsan_port_calls(느슨한 facility_name LIKE 매칭)를
        # mart.facility_alias 기준 분류로 대체 — P1 정규화를 거친 값이라 더
        # 정확하다. 온산/울산 지역 구분이 아니라 "선석으로 확인됐는가"
        # 기준이라 개념이 살짝 다르니 필드명을 그대로 옮기지 않고 새로 둔다.
        "port_calls_by_facility_type": {row["facility_type"]: row["n"] for row in facility_type_rows},
        "liquid_callsgns": list(liquid_callsgns),
    }


# --------------------------------------------------------------------------
# 관제 경고 센터 — 재항 현황을 safety 규칙엔진에 통과시켜 만든 상시 경고.
#
# /safety/assess 는 "이 화물을 배정해도 되나"를 묻는 요청형이라, 아무도 묻지
# 않으면 아무것도 알려주지 않는다. 경고 센터는 그 반대가 필요하다 — 지금 이미
# 붙어 있는 위험한 조합을 스스로 찾아 띄워야 한다.
#
# 판정은 전부 safety 에이전트 함수를 그대로 재사용한다(berth_alerts.py 주석 참고).
# 프론트(frontend/mock-server)가 "IMDG 등급 2종 동시 재항 — 확인 필요"까지만
# 말할 수 있었던 건 그쪽에 판정 권위가 없어서다. 여기서는 실제 등급이 나온다.
# --------------------------------------------------------------------------


@router.get("/alerts", summary="관제 경고 목록 조회")
async def get_dashboard_alerts(db: AsyncSession = Depends(get_session)) -> list[dict]:
    return await _cached("alerts", lambda: _get_dashboard_alerts_impl(db))


async def _get_dashboard_alerts_impl(db: AsyncSession) -> list[dict]:
    """재항 화물 혼재금지·IMDG 격리·흘수 위반 경고. 0건이면 위험 없음(정상)."""
    return await build_berth_alerts(db, neo4j_client.driver)


# --------------------------------------------------------------------------
# 다차원 안전 평가 지수 — 관제 화면 레이더 차트용.
#
# 화면의 6축이 원래 탱크 압력·가스 농도 등 우리가 수집하지 않는 센서값이라
# 하드코딩돼 있었다. 실제로 계산 가능한 안전 차원으로 축을 바꾸고, 각 축마다
# 그 점수가 나온 원자료(basis)를 함께 내려준다 (safety_index.py 주석 참고).
# 재료가 없는 축은 0/100 이 아니라 null 이다 — "모름"을 "안전"으로 만들지 않는다.
# --------------------------------------------------------------------------


@router.get("/safety-index", summary="다차원 안전 평가 지수 조회")
async def get_safety_index(db: AsyncSession = Depends(get_session)) -> dict:
    """기상·흘수·혼재·화물식별·선석특정·신선도 6축 점수(0~100)와 근거."""
    return await _cached("safety-index", lambda: build_safety_index(db, neo4j_client.driver))


# --------------------------------------------------------------------------
# 선석 현황 — upa_berth_facility + AIS 실측 접안 + 최신 판정, 슬롯 단위.
#
# [2026-09-21] 이 화면의 질문이 바뀌었다.
#   이전: "우리 시스템이 이 선석에 무엇을 배정(추천/승인)했는가"
#   지금: "이 선석에 지금 무엇이 붙어 있고, 그게 조건에 맞는가"
# 우리는 배정하지 않으므로(방향 C) 보여줄 배정이 없다. 대신 관측(누가 붙어
# 있나)과 판정(맞나)을 겹쳐 보여준다.
#
# [2026-09-22] 점유 근거를 mart.vessel_presence 로 통일했다.
#   한동안 이 화면만 mart.berth_occupancy_live(별도 뷰)를 봤다. 위 /berths 가
#   upa_port_call(사후 이력)을 쓰던 시절엔 그 편이 나았지만, /berths 도 이제
#   vessel_presence 를 쓴다 — 같은 질문에 뷰가 둘이면 화면마다 척수가 달라진다.
#
#   실측(2026-09-22, 같은 스냅샷)으로 실제로 갈렸다:
#       vessel_presence      접안 14척
#       berth_occupancy_live 접안  0척
#   후자는 now() 기준 30분 안의 위치만 '접안'으로 봤는데 최신 위치가 6시간 33분
#   전이라 전부 NO_SIGNAL 로 떨어졌다 — 수집이 잠깐만 밀려도 이 화면이 통째로
#   빈다. vessel_presence 는 최신 스냅샷 기준으로 판정하고 낡은 정도를
#   quality_flag·position_age_min 으로 따로 밝힌다.
#
# /berths 와 이 화면의 차이는 이제 소스가 아니라 **질문**이다 — 저쪽은 "어디에
# 몇 척", 이쪽은 "그 배가 이 자리에 맞는가"(판정 오버레이).
#
# 빈 슬롯도 slot_no 1..max_concurrent_vessels 전부 채워서 내려준다 — 프론트가
# "몇 자리 중 몇 자리가 찼는지"를 계산 없이 바로 그릴 수 있게.
#
# [2026-08-21] 온산 MVP 스코프(15개 선석)로 범위를 좁혔다 — 스케줄링 에이전트가
# onsan_scope 하드 필터로 이 선석에만 배정하도록 바뀌었고(graph_queries.py
# _CYPHER_FIND_ELIGIBLE_BERTHS), 이 화면과 같은 지도를 쓰는 PortMap.jsx도 이미
# 이 범위만 그린다 — 이 화면만 다른 범위를 보여주면 "지도에 없던 선석이
# 배정현황엔 있다"는 불일치가 생긴다.
#
# WHERE b.port_name = '온산항'이 아니라 명시적 wharf_name 목록을 쓰는 이유
# (2026-08-21 실DB 확인) — port_name='온산항'은 실제로 20개 시설을 반환하는데,
# 그중 5개(정일컨부두·온산1~4부두)는 컨테이너/광석/시멘트를 취급하는 비-액체화물
# 부두라 스케줄링 에이전트가 절대 배정하지 않는다(Neo4j onsan_scope=false).
# 그 5개까지 지도에 넣으면 "이 부두는 왜 항상 비어 있나"는 새로운 불일치가
# 생기므로, ONSAN_SCOPE_WHARF_NAMES(data-pipeline/berth_neo4j_loader.py)와
# 정확히 같은 15개 이름을 여기 그대로 옮긴다. 이 쿼리는 Postgres만 조회해서
# Neo4j의 onsan_scope 속성을 직접 쓸 수 없어 목록이 두 곳에 중복된다 — 온산
# 스코프를 바꿀 때는 반드시 두 곳(이 목록과 ONSAN_SCOPE_WHARF_NAMES)을 함께
# 고친다.
_ONSAN_SCOPE_WHARF_NAMES = (
    "OTK1부두", "OTK2부두", "정일1부두", "정일2부두", "UTK부두", "대한유화부두",
    "효성부두", "달포부두", "S-Oil 1부두", "S-Oil 2부두", "S-Oil 3부두", "S-Oil 4부두",
    "S-Oil부이", "S-Oil&오일허브 부이", "석유공사부이",
)
# --------------------------------------------------------------------------

_QUERY_BERTH_ASSIGNMENTS = text("""
    WITH live AS (
        -- 슬롯은 우리가 나눠 주는 자리가 아니라 **지금 붙어 있는 배를 센 결과**다.
        -- 부두에 가까운 순으로 번호를 매긴다 — 실제 선석 번호가 아니라 표시 순서다.
        -- 거리가 없는 배(좌표 없는 부이 등 '신고'만으로 잡힌 건)는 뒤로 보낸다.
        SELECT vp.berth_name AS wharf_name, vp.callsgn, vp.vessel_name,
               vp.berth_dist_m AS distance_m, vp.berth_basis,
               vp.received_at_utc, vp.quality_flag, vp.position_age_min,
               row_number() OVER (
                   PARTITION BY vp.berth_name
                   ORDER BY vp.berth_dist_m NULLS LAST, vp.callsgn
               )::int AS slot_no
        FROM mart.vessel_presence vp
        WHERE vp.presence_zone = 'BERTH' AND vp.berth_name IS NOT NULL
    ),
    latest_assessment AS (
        SELECT DISTINCT ON (call_sign) call_sign, id, stage, level, action, recipient,
               reasons, axes, acknowledged_by, assessed_at_utc
        FROM assessment_history
        WHERE assessed_at_utc > now() - interval '48 hours'
        ORDER BY call_sign, assessed_at_utc DESC
    )
    SELECT b.wharf_name, b.latitude, b.longitude,
           COALESCE(b.berth_vessel_count, 1)::int AS max_concurrent_vessels,
           b.port_name, b.length_m, b.depth_m, b.berth_capacity AS max_dwt,
           b.port_operator_name AS operator,
           -- 원천 대분류가 아니라 보정된 취급화물(mart_views.sql 12절).
           -- 원천 값을 쓰면 스케줄링 에이전트(그래프)와 이 화면이 어긋난다 —
           -- 가스부두가 판정은 '가스', 화면 표시는 '유류' 로 나왔다.
           bhc.handling_cargo_name, b.wharf_se_name,
           lv.slot_no, lv.callsgn AS call_sign, lv.vessel_name,
           lv.distance_m, lv.received_at_utc AS position_at_utc,
           -- 점유 판정 근거와 신선도 — 화면이 "왜 여기 있다고 보나"와
           -- "그 판단이 얼마나 최근 것인가"를 그대로 보여줄 수 있게.
           lv.berth_basis, lv.quality_flag, lv.position_age_min,
           -- 판정 — 이 배가 이 자리에 맞는가. 없으면 전부 NULL(아직 판정 전).
           la.id AS assessment_id, la.stage, la.level, la.action, la.recipient,
           la.reasons, la.acknowledged_by, la.assessed_at_utc,
           cm.chem_id AS cargo_chem_id, mc.name_ko AS cargo_name,
           -- 실제 입출항은 PORT-MIS(공식 신고)를 우선하고 VTS 로 보충한다.
           -- 다만 PORT-MIS 는 수집창 [어제, 오늘+3일] 밖이면 동결되므로
           -- portmis_collected_at 을 함께 내려보내 화면이 신선도를 알 수 있게 한다.
           pm.arrival_at_utc AS actual_arrival_utc,
           pm.departure_at_utc AS actual_departure_utc,
           pm.departure_sched_utc AS departure_scheduled_utc,
           pm.collected_at_utc AS portmis_collected_at
    FROM upa_berth_facility b
    -- record_uid 로 조인했더니 늘 빈 배열이었다(2026-08-21 실측) — upa_berth_facility 는
    -- wharf_name 자연키로 전환돼 record_uid 가 항상 NULL 이고 NULL=NULL 은 거짓이다.
    -- mart.berth_handling_cargo 는 이 표를 그대로 SELECT 한 뷰라
    -- (wharf_name, port_operator_name) 이 유일키다(SK2부두처럼 이름이 겹쳐도 운영사가 다름).
    JOIN mart.berth_handling_cargo bhc
      ON bhc.wharf_name = b.wharf_name
     AND bhc.port_operator_name IS NOT DISTINCT FROM b.port_operator_name
    -- [2026-09-21] berth_assignment(우리 예약) -> 실측 접안.
    --   이 화면의 질문이 바뀌었다. 전에는 "우리가 이 선석에 무엇을 배정했나"였고
    --   지금은 **"이 선석에 지금 무엇이 붙어 있고, 그게 조건에 맞는가"** 다.
    --   vessel_presence.berth_name 은 이미 wharf 정본 표기라 facility_alias 를
    --   거치지 않는다 (별칭 조인은 척수를 뻥튀기한다 — 실측 2부두 3척 -> 9척).
    LEFT JOIN live lv ON lv.wharf_name = b.wharf_name
    LEFT JOIN latest_assessment la ON upper(btrim(la.call_sign)) = upper(btrim(lv.callsgn))
    LEFT JOIN LATERAL (
        SELECT cm2.chem_id FROM mart.cargo_msds cm2
        WHERE upper(btrim(cm2.callsgn)) = upper(btrim(lv.callsgn)) AND cm2.chem_id IS NOT NULL
        -- ORDER BY 없는 LIMIT 1 은 한 배에 화물이 여럿일 때 어느 물질이 뽑힐지
        -- 우연에 맡긴다 — 인화점·IMDG 등급이 달라 화면 표시가 실제로 흔들린다.
        ORDER BY cm2.chem_id
        LIMIT 1
    ) cm ON true
    LEFT JOIN msds_chemical mc ON mc.chem_id = cm.chem_id
    LEFT JOIN LATERAL (
        SELECT pv.arrival_at_utc, pv.departure_at_utc, pv.departure_sched_utc, pv.collected_at_utc
        FROM portmis_vessel pv
        WHERE lv.callsgn IS NOT NULL
          AND upper(btrim(pv.callsgn)) = upper(btrim(lv.callsgn))
          AND pv.arrival_at_utc IS NOT NULL
        ORDER BY pv.arrival_at_utc DESC
        LIMIT 1
    ) pm ON true
    WHERE b.wharf_name = ANY(CAST(:onsan_wharf_names AS text[]))
    ORDER BY b.wharf_name, lv.slot_no
""")


@router.get("/berth-assignments", summary="선석 현황 조회 (실측 접안 + 최신 판정, 슬롯 단위)")
async def get_berth_assignments(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """선석별로 지금 붙어 있는 배와 그 배의 최신 판정.

    `slots[].status` 에는 배정 상태가 아니라 **판정 등급**(적합/주의/부적합/판정불가)이
    들어간다. 아직 판정 전이면 None 이다 — 배는 붙어 있는데 판정이 없다는 뜻이고,
    그것도 관제사가 알아야 할 사실이라 숨기지 않는다.

    확인 액션은 `POST /approvals/{assessment_id}/acknowledge` 다. 승인이 아니라
    "이 판정을 봤다"는 기록이며, 눌러도 아무 자원이 잠기지 않는다.
    """
    rows = (
        await db.execute(
            _QUERY_BERTH_ASSIGNMENTS, {"onsan_wharf_names": list(_ONSAN_SCOPE_WHARF_NAMES)}
        )
    ).mappings().all()

    berths: dict[str, dict] = {}
    occupied_slot_nos: dict[str, set[int]] = {}
    for row in rows:
        name = row["wharf_name"]
        entry = berths.setdefault(name, {
            "wharf_name": name,
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "max_concurrent_vessels": row["max_concurrent_vessels"],
            # 선석 기본정보(upa_berth_facility) — 지도에서 선석 클릭 시 표시용
            "port_name": row["port_name"],
            "length_m": row["length_m"],
            "depth_m": row["depth_m"],
            "max_dwt": row["max_dwt"],
            "operator": row["operator"],
            "handling_cargo_name": row["handling_cargo_name"],
            "wharf_se_name": row["wharf_se_name"],
            "slots": [],
        })
        if row["slot_no"] is not None:
            # 이 슬롯은 예약이 아니라 **지금 붙어 있는 배**다. status 자리에는
            # 배정 상태가 아니라 판정 등급이 들어간다 — 아직 판정 전이면 None.
            entry["slots"].append({
                "slot_no": row["slot_no"],
                "assessment_id": row["assessment_id"],
                "status": row["level"],
                "stage": row["stage"],
                "action": row["action"],
                "recipient": row["recipient"],
                "reasons": row["reasons"],
                "acknowledged_by": row["acknowledged_by"],
                "assessed_at_utc": row["assessed_at_utc"],
                "call_sign": row["call_sign"],
                "vessel_name": row["vessel_name"],
                "cargo_chem_id": row["cargo_chem_id"],
                "cargo_name": row["cargo_name"],
                # 실측 접안 근거 — 판정 방식('신고+위치'/'위치'/'신고'), 부두
                # 대표좌표까지의 거리, 관측 시각, 그리고 그 관측의 신선도.
                "berth_basis": row["berth_basis"],
                "distance_m": row["distance_m"],
                "position_at_utc": row["position_at_utc"],
                "quality_flag": row["quality_flag"],
                "position_age_min": row["position_age_min"],
                "actual_arrival_utc": row["actual_arrival_utc"],
                "actual_departure_utc": row["actual_departure_utc"],
                "departure_scheduled_utc": row["departure_scheduled_utc"],
                # PORT-MIS 수집창 [어제, 오늘+3일] 밖이면 이 값이 며칠 전에서 멈춘다.
                # 화면이 "이 입출항 정보가 얼마나 묵었나"를 표시할 수 있게 함께 내린다.
                "portmis_collected_at": row["portmis_collected_at"],
            })
            occupied_slot_nos.setdefault(name, set()).add(row["slot_no"])

    # 빈 슬롯 채우기(status=null) — 실제 배정이 걸린 slot_no와 안 겹치게 채운다
    for name, entry in berths.items():
        occupied = occupied_slot_nos.get(name, set())
        for slot_no in range(1, entry["max_concurrent_vessels"] + 1):
            if slot_no not in occupied:
                entry["slots"].append({"slot_no": slot_no, "status": None})
        entry["slots"].sort(key=lambda s: s["slot_no"])

    return list(berths.values())
