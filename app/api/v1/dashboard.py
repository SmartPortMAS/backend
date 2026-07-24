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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.models.environmental_obs import TideObs, WaveObs, WeatherObs
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# --------------------------------------------------------------------------
# 기상/조위/파고 현황
# --------------------------------------------------------------------------

_OBS_MAX_AGE_HOURS = 3  # weather 에이전트의 MAX_STALENESS와 동일 기준


def _factor(row, value_attr: str, unit: str, as_of: datetime) -> dict:
    if row is None:
        return {"value": None, "unit": unit, "observed_at_utc": None, "station_name": None, "is_stale": True}
    observed_at = row.observed_at_utc
    is_stale = observed_at is None or (as_of - observed_at).total_seconds() > _OBS_MAX_AGE_HOURS * 3600
    return {
        "value": getattr(row, value_attr),
        "unit": unit,
        "observed_at_utc": observed_at,
        "station_name": row.station_name,
        "is_stale": is_stale,
    }


@router.get("/weather")
async def get_weather_overview(db: AsyncSession = Depends(get_session)) -> dict:
    """풍속/파고/조위 최신 관측치 한 화면분(참고용 — 판정은 /api/v1/weather/assess에서)."""
    as_of = datetime.now(timezone.utc)

    weather_row = await db.scalar(select(WeatherObs).order_by(WeatherObs.observed_at_utc.desc()).limit(1))
    wave_row = await db.scalar(select(WaveObs).order_by(WaveObs.observed_at_utc.desc()).limit(1))
    tide_row = await db.scalar(select(TideObs).order_by(TideObs.observed_at_utc.desc()).limit(1))

    return {
        "wind": _factor(weather_row, "wind_speed_ms", "m/s", as_of),
        "wave": _factor(wave_row, "wave_height_sig_m", "m", as_of),
        "tide": _factor(tide_row, "tide_level_cm", "cm", as_of),
        "air_temp_c": weather_row.air_temp_c if weather_row else None,
        "humidity_pct": weather_row.humidity_pct if weather_row else None,
        "visibility_m": weather_row.visibility_m if weather_row else None,
    }


# --------------------------------------------------------------------------
# 선석 현황 (Neo4j Berth + Postgres upa_port_call 점유 현황)
# --------------------------------------------------------------------------

_CYPHER_ALL_BERTHS = """
MATCH (b:Berth)
OPTIONAL MATCH (b)-[:HANDLES]->(cat:CargoCategory)
RETURN b.id AS berth_id, b.wharf_name AS wharf_name, b.port_name AS port_name,
       b.port_operator_name AS operator, b.depth_m AS depth_m, b.berth_group AS berth_group,
       b.latitude AS latitude, b.longitude AS longitude,
       collect(DISTINCT cat.name) AS categories
"""

# 지금 이 순간(point-in-time) 점유 중인 시설(선석/정박지 공통)을 한 번에 집계.
# scheduling/occupancy.py의 find_overlapping_port_calls와 같은 원칙(DISTINCT ON
# port_call_id)이지만, 대시보드는 특정 wharf_names 목록이 아니라 전체를 한 번에 봐야
# 해서 facility_name별로 GROUP BY까지 한 번에 처리하는 별도 집계 쿼리를 쓴다.
_QUERY_CURRENT_OCCUPANCY_ALL = text("""
    WITH latest_calls AS (
        SELECT DISTINCT ON (port_call_id)
            port_call_id, facility_name, vessel_name, arrival_at_utc, departure_at_utc
        FROM upa_port_call
        WHERE arrival_at_utc IS NOT NULL
          AND arrival_at_utc < :now
          AND (departure_at_utc IS NULL OR departure_at_utc > :now)
        ORDER BY port_call_id, arrival_at_utc
    )
    SELECT facility_name, count(*) AS occupant_count,
           array_agg(vessel_name ORDER BY arrival_at_utc DESC) AS vessel_names
    FROM latest_calls
    GROUP BY facility_name
""")


@router.get("/berths")
async def get_berths_overview(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """전체 선석 목록 + 취급화물 + 실시간 점유 현황(upa_port_call 실측)."""
    now = datetime.now(timezone.utc)

    async with neo4j_client.driver.session() as session:
        async def _tx(tx):
            result = await tx.run(_CYPHER_ALL_BERTHS)
            return [record.data() async for record in result]

        berths = await session.execute_read(_tx)

    occupancy_rows = (await db.execute(_QUERY_CURRENT_OCCUPANCY_ALL, {"now": now})).mappings().all()
    occupancy_by_facility = {row["facility_name"]: row for row in occupancy_rows}

    out = []
    for b in berths:
        occ = occupancy_by_facility.get(b["wharf_name"])
        out.append({
            **b,
            "occupancy_status": "점유" if occ else "여유",
            "current_vessel_names": occ["vessel_names"] if occ else [],
        })
    return out


# --------------------------------------------------------------------------
# 정박지 현황 (Neo4j Anchorage + upa_port_call '정박지-E1' 등 재선 이력)
# --------------------------------------------------------------------------

_CYPHER_ALL_ANCHORAGES = """
MATCH (a:Anchorage)
RETURN a.id AS anchorage_id, a.name AS name, a.tonnage_rule AS tonnage_rule,
       a.anchorage_type AS anchorage_type, a.latitude AS latitude, a.longitude AS longitude
"""

# app.agents.scheduling.service.ANCHORAGE_ID_TO_PORT_CALL_FACILITY와 동일 매핑
# (E1/E2/E3만 실측 확인됨, 갭분석 2-2절). 대시보드도 같은 제약을 그대로 따른다.
_ANCHORAGE_FACILITY_MAP = {"E1": "정박지-E1", "E2": "정박지-E2", "E3": "정박지-E3"}


@router.get("/anchorages")
async def get_anchorages_overview(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """전체 정박지 목록 + 실시간 대기 척수(E1/E2/E3만 확인됨, 나머지는 null)."""
    now = datetime.now(timezone.utc)

    async with neo4j_client.driver.session() as session:
        async def _tx(tx):
            result = await tx.run(_CYPHER_ALL_ANCHORAGES)
            return [record.data() async for record in result]

        anchorages = await session.execute_read(_tx)

    facility_names = list(_ANCHORAGE_FACILITY_MAP.values())
    occupancy_rows = (
        await db.execute(
            text("""
                WITH latest_calls AS (
                    SELECT DISTINCT ON (port_call_id)
                        port_call_id, facility_name, arrival_at_utc, departure_at_utc
                    FROM upa_port_call
                    WHERE facility_name = ANY(:facility_names)
                      AND arrival_at_utc IS NOT NULL
                      AND arrival_at_utc < :now
                      AND (departure_at_utc IS NULL OR departure_at_utc > :now)
                    ORDER BY port_call_id, arrival_at_utc
                )
                SELECT facility_name, count(*) AS occupant_count FROM latest_calls GROUP BY facility_name
            """),
            {"facility_names": facility_names, "now": now},
        )
    ).mappings().all()
    count_by_facility = {row["facility_name"]: row["occupant_count"] for row in occupancy_rows}

    out = []
    for a in anchorages:
        facility_name = _ANCHORAGE_FACILITY_MAP.get(a["anchorage_id"])
        out.append({
            **a,
            "current_occupants": count_by_facility.get(facility_name) if facility_name else None,
        })
    return out


# --------------------------------------------------------------------------
# 선박 위치 (upa_vessel_position — getVslPstnInfo, 울산 항내 스코프)
# --------------------------------------------------------------------------

_QUERY_LATEST_VESSEL_POSITIONS = text("""
    SELECT DISTINCT ON (callsgn)
        callsgn, vessel_name, mmsi, imo_no, latitude, longitude,
        sog, cog, heading, draught, nav_status_code, received_at_utc
    FROM upa_vessel_position
    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    ORDER BY callsgn, received_at_utc DESC
""")


@router.get("/vessels")
async def get_vessel_positions(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """선박별 최신 위치 1건(지도 표시용). 좌표 결측 행은 제외."""
    rows = (await db.execute(_QUERY_LATEST_VESSEL_POSITIONS)).mappings().all()
    return [dict(row) for row in rows]
