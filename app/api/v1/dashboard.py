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
# 선석 현황 (Neo4j Berth + Postgres upa_port_call 점유 현황)
#
# ★ mart.facility_alias 경유로 조인한다 (P1 처방). VTS 운항관제
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
    WITH latest_calls AS (
        SELECT DISTINCT ON (port_call_id)
            port_call_id, facility_name, vessel_name, arrival_at_utc, departure_at_utc
        FROM upa_port_call
        WHERE arrival_at_utc IS NOT NULL
          AND arrival_at_utc < :now
          AND (departure_at_utc IS NULL OR departure_at_utc > :now)
        ORDER BY port_call_id, arrival_at_utc
    )
    SELECT fa.wharf_name, count(*) AS occupant_count,
           array_agg(lc.vessel_name ORDER BY lc.arrival_at_utc DESC) AS vessel_names
    FROM latest_calls lc
    JOIN mart.facility_alias fa
      ON fa.source_name = lc.facility_name AND fa.facility_type = 'BERTH'
    GROUP BY fa.wharf_name
""")


@router.get("/berths", summary="선석 목록 및 실시간 점유 현황")
async def get_berths_overview(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """전체 선석 목록 + 취급화물 + 실시간 점유 현황(upa_port_call, facility_alias 매칭)."""
    now = datetime.now(timezone.utc)

    async with neo4j_client.driver.session() as session:
        async def _tx(tx):
            result = await tx.run(_CYPHER_ALL_BERTHS)
            return [record.data() async for record in result]

        berths = await session.execute_read(_tx)

    occupancy_rows = (
        await db.execute(_QUERY_BERTH_OCCUPANCY, {"now": now})
    ).mappings().all()
    # 같은 wharf_name을 가진 Neo4j Berth 노드가 여럿일 수 있다(예: 'SK2부두(민유)'와
    # 'SK2부두(국유)' — 마스터에 선석번호 구분이 없는 채로 두 노드가 있는 경우).
    # 그 경우 같은 점유 현황을 양쪽 다 보여준다 — 어느 쪽인지 구분할 근거가 없는
    # 걸 숨기지 않는다(P1 한계로 남겨둠).
    occupancy_by_wharf = {row["wharf_name"]: row for row in occupancy_rows}

    out = []
    for b in berths:
        occ = occupancy_by_wharf.get(b["wharf_name"])
        out.append({
            **b,
            "occupancy_status": "점유" if occ and occ["occupant_count"] else "여유",
            "current_vessel_names": occ["vessel_names"] if occ and occ["occupant_count"] else [],
        })
    return out


@router.get("/berths/unmapped", summary="선석으로 매칭되지 않은 시설의 재항 현황")
async def get_unmapped_facility_calls(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """facility_alias가 선석으로 매칭 못한 시설명별 현재 재항 척수(P1 잔여 갭 노출용).

    대부분 '정박지 01'~'07'(Neo4j Anchorage 어느 코드와 대응하는지 근거자료 없음)
    과 '현대오일터미널신항부두'(신항1·2부두 중 어느 쪽인지 표기로 판별 불가)다.
    억지로 맞추지 않고 그대로 노출한다 — mart_views.sql 0-B절 참고.
    """
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            text("""
                WITH latest_calls AS (
                    SELECT DISTINCT ON (port_call_id)
                        port_call_id, facility_name, arrival_at_utc, departure_at_utc
                    FROM upa_port_call
                    WHERE arrival_at_utc IS NOT NULL AND arrival_at_utc < :now
                      AND (departure_at_utc IS NULL OR departure_at_utc > :now)
                    ORDER BY port_call_id, arrival_at_utc
                )
                SELECT lc.facility_name, fa.facility_type, count(*) AS occupant_count
                FROM latest_calls lc
                JOIN mart.facility_alias fa ON fa.source_name = lc.facility_name
                WHERE fa.facility_type IN ('UNMAPPED', 'OTHER')
                GROUP BY lc.facility_name, fa.facility_type
                ORDER BY occupant_count DESC
            """),
            {"now": now},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 정박지 현황 (Neo4j Anchorage + upa_port_call 재선 이력, mart.facility_alias 경유)
#
# 예전엔 E1/E2/E3 매핑을 이 파일에 하드코딩했다. 지금은 mart.facility_alias의
# facility_type='ANCHORAGE' 행(정박지-E1 → E1 등)에서 그대로 가져온다 —
# 매핑이 하나 늘어도(예: 정박지 01~07의 대응이 나중에 확인되면) mart_views.sql
# 쪽 수동 별칭만 추가하면 되고 backend 코드는 안 건드려도 된다.
# '정박지 01'~'07'은 아직 대응 근거가 없어(mart_views.sql 0-B절) 이 조인에
# 안 걸리고, current_occupants는 여전히 null로 남는다 — 억지로 맞추지 않는다.
# --------------------------------------------------------------------------

_CYPHER_ALL_ANCHORAGES = """
MATCH (a:Anchorage)
RETURN a.id AS anchorage_id, a.name AS name, a.tonnage_rule AS tonnage_rule,
       a.anchorage_type AS anchorage_type, a.latitude AS latitude, a.longitude AS longitude
"""

_QUERY_ANCHORAGE_OCCUPANCY = text("""
    WITH latest_calls AS (
        SELECT DISTINCT ON (port_call_id)
            port_call_id, facility_name, arrival_at_utc, departure_at_utc
        FROM upa_port_call
        WHERE arrival_at_utc IS NOT NULL
          AND arrival_at_utc < :now
          AND (departure_at_utc IS NULL OR departure_at_utc > :now)
        ORDER BY port_call_id, arrival_at_utc
    )
    SELECT fa.anchorage_key, count(*) AS occupant_count
    FROM latest_calls lc
    JOIN mart.facility_alias fa
      ON fa.source_name = lc.facility_name AND fa.facility_type = 'ANCHORAGE'
    GROUP BY fa.anchorage_key
""")


@router.get("/anchorages", summary="정박지 목록 및 실시간 대기 현황")
async def get_anchorages_overview(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """전체 정박지 목록 + 실시간 대기 척수(mart.facility_alias에 매핑된 것만, 나머지는 null)."""
    now = datetime.now(timezone.utc)

    async with neo4j_client.driver.session() as session:
        async def _tx(tx):
            result = await tx.run(_CYPHER_ALL_ANCHORAGES)
            return [record.data() async for record in result]

        anchorages = await session.execute_read(_tx)

    occupancy_rows = (
        await db.execute(_QUERY_ANCHORAGE_OCCUPANCY, {"now": now})
    ).mappings().all()
    count_by_anchorage = {row["anchorage_key"]: row["occupant_count"] for row in occupancy_rows}

    out = []
    for a in anchorages:
        out.append({
            **a,
            "current_occupants": count_by_anchorage.get(a["anchorage_id"]),
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


@router.get("/vessels", summary="선박 실시간 위치 조회")
async def get_vessel_positions(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """선박별 최신 위치 1건(지도 표시용). 좌표 결측 행은 제외."""
    rows = (await db.execute(_QUERY_DASHBOARD_CURRENT_VESSELS)).mappings().all()
    return [dict(row) for row in rows]


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
           tide_observed_at_utc, draught_observed_at_utc, arrival_at_utc
    FROM mart.berth_draught_check
    ORDER BY CASE draught_verdict
                 WHEN 'NOT_ALLOWED' THEN 0
                 WHEN 'MARGINAL' THEN 1
                 WHEN 'UNKNOWN' THEN 2
                 ELSE 3
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
    return await build_safety_index(db, neo4j_client.driver)


# --------------------------------------------------------------------------
# 선석 배정현황 — berth + berth_assignment 조인, 슬롯 단위.
#
# 08_스케줄링_전면재설계_자동배정_설계문서.md §7 — 위 /berths(upa_port_call
# VTS 관측 기준 "실제로 배가 있는가")와는 다른 질문에 답한다. 이건 "우리
# 시스템이 이 선석에 무엇을 배정(추천/승인)했는가"를 보여준다 — 데이터
# 출처가 다르므로 /berths를 확장하지 않고 새 엔드포인트로 분리했다(§7.1).
#
# 빈 슬롯도 slot_no 1..max_concurrent_vessels 전부 채워서 내려준다 — 프론트가
# "몇 개 슬롯 중 몇 개가 찼는지"를 계산 없이 바로 그릴 수 있게.
# --------------------------------------------------------------------------

_QUERY_BERTH_ASSIGNMENTS = text("""
    SELECT b.wharf_name, b.latitude, b.longitude,
           COALESCE(b.berth_vessel_count, 1)::int AS max_concurrent_vessels,
           b.port_name, b.length_m, b.depth_m, b.berth_capacity AS max_dwt,
           b.port_operator_name AS operator, b.handling_cargo_name, b.wharf_se_name,
           ba.id AS assignment_id,
           ba.slot_no, ba.status, ba.call_sign, ba.vessel_name,
           ba.cargo_chem_id, mc.name_ko AS cargo_name,
           lower(ba.planned_window) AS window_start, upper(ba.planned_window) AS window_end,
           -- 실제 입항은 ba.actual_berthing_at을 우선 쓴다(2026-08-20 — arrival_watcher가
           -- 배정 생성 시점에 이미 VTS 확인 입항시각을 직접 채워 둔다, 이 배정 자신의
           -- 값이라 재항 건 혼동이 없다). 그 다음은 portmis_vessel(공식 신고, 2026-08-20
           -- details 파싱 복원 이후 사용 가능해짐) — VTS(upa_port_call)보다 신뢰도가
           -- 높다. 아래 두 LATERAL 모두 없는 배정만 VTS로 마지막 보충한다.
           COALESCE(ba.actual_berthing_at, pm.arrival_at_utc, pc.arrival_at_utc) AS actual_arrival_utc,
           COALESCE(ba.actual_departure_at, pm.departure_at_utc, pc.departure_at_utc) AS actual_departure_utc,
           -- PORT-MIS만 주는 값 — 입항 시점에 선사가 신고하는 출항 "예정" 시각.
           -- 아직 출항 전이어도(actual_departure_utc가 null이어도) 언제쯤 비는지
           -- 미리 보여줄 수 있다("출항시간 기준으로 자리를 비워야" 요청, 2026-08-20).
           pm.departure_sched_utc AS departure_scheduled_utc,
           ba.assignment_reason, ba.approved_by, ba.rejected_candidates AS decision_detail
    FROM upa_berth_facility b
    LEFT JOIN berth_assignment ba
      ON ba.berth_id = b.wharf_name
     AND ba.status IN ('REQUESTED', 'APPROVED', 'SCHEDULED', 'BERTHED')
     -- 관제사가 승인/반려하지 않고 방치한 REQUESTED 추천은 계획기간(planned_window)이
     -- 이미 끝나면 뺀다(2026-08-20, 실사용 중 발견 — SK5부두 슬롯 1에 8/11·8/18·8/20
     -- 세 건이 동시에 "점유 중"으로 뜸. 셋 다 서로 겹치지 않는 시간대라 EXCLUDE
     -- 제약은 정상 작동한 것이었고, 문제는 출항 확인 이벤트가 없어 release_
     -- completed_berths가 못 닫는 낡은 REQUESTED가 계속 쌓이는 쪽이었다). APPROVED
     -- 이후 상태는 관제사가 이미 확정한 실제 점유이므로 계획기간이 지나도(실제 출항
     -- 지연 등) 계속 보여준다 — 여기서 거르는 건 "아무도 결정하지 않은 채 시효가
     -- 지난 추천"뿐이다. DB 행 자체는 감사 기록으로 남고 지우지 않는다.
     AND (ba.status != 'REQUESTED' OR ba.planned_window IS NULL OR upper(ba.planned_window) > now())
    LEFT JOIN msds_chemical mc ON mc.chem_id = ba.cargo_chem_id
    -- PORT-MIS(공식 신고) 실제 입출항 + 출항예정 — call_sign 기준으로 이 배의
    -- 가장 최근 방문 하나만 붙인다(같은 배가 재항을 여러 번 했을 수 있어서).
    -- 계획 구간과 겹치거나 가까운(2일 버퍼) 건만 인정해 다른 방문과 혼동을 막는다
    -- (아래 VTS LATERAL과 동일 원칙 — 실측으로 재현했던 "지난달 기록이 붙는" 사고).
    LEFT JOIN LATERAL (
        SELECT pv.arrival_at_utc, pv.departure_at_utc, pv.departure_sched_utc
        FROM portmis_vessel pv
        WHERE ba.call_sign IS NOT NULL
          AND upper(btrim(pv.callsgn)) = upper(btrim(ba.call_sign))
          AND pv.arrival_at_utc IS NOT NULL
          AND pv.arrival_at_utc > lower(ba.planned_window) - interval '2 days'
          AND pv.arrival_at_utc < upper(ba.planned_window) + interval '2 days'
        ORDER BY pv.arrival_at_utc DESC
        LIMIT 1
    ) pm ON true
    -- VTS(upa_port_call) 실제 입출항 — PORT-MIS·ba 실측값이 전부 없을 때만 쓰는
    -- 마지막 폴백(2026-08-19, "선박 정보에 입항/출항시간 있지 않냐"는 지적).
    LEFT JOIN LATERAL (
        SELECT pc2.arrival_at_utc, pc2.departure_at_utc
        FROM upa_port_call pc2
        WHERE ba.call_sign IS NOT NULL
          AND upper(btrim(pc2.callsgn)) = upper(btrim(ba.call_sign))
          AND pc2.arrival_at_utc IS NOT NULL
          AND pc2.arrival_at_utc > lower(ba.planned_window) - interval '2 days'
          AND pc2.arrival_at_utc < upper(ba.planned_window) + interval '2 days'
        ORDER BY pc2.arrival_at_utc DESC
        LIMIT 1
    ) pc ON true
    ORDER BY b.wharf_name, ba.slot_no
""")


@router.get("/berth-assignments", summary="선석 배정현황 조회 (슬롯 단위, §7.2)")
async def get_berth_assignments(db: AsyncSession = Depends(get_session)) -> list[dict]:
    """선석별 슬롯 배정 상태. REQUESTED(추천, 승인 대기)/APPROVED 이후(확정)를 구분해서
    보여준다 — 승인 액션은 POST /approvals/{id}/decision.

    upa_berth_facility.wharf_name은 UNIQUE라 선석당 행이 하나뿐이다(2026-08-19,
    berth_assignment.berth_id FK를 여기로 옮기면서 중복 berth 마스터 행 문제 자체가
    없어짐 — 이전에는 berth 테이블에 같은 wharf_name 중복 행이 생겨 지도에 원이
    여러 개 겹쳐 찍히는 문제가 있었다).
    """
    rows = (await db.execute(_QUERY_BERTH_ASSIGNMENTS)).mappings().all()

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
            entry["slots"].append({
                "slot_no": row["slot_no"],
                # 읽기전용 표시용 — 실제 승인/반려는 이 페이지가 아니라 종합에이전트
                # 콘솔(AgentConsole)이 GET /approvals/pending으로 같은 id를 조회해
                # POST /approvals/{id}/decision을 부른다(2026-08-19, 이 페이지에 있던
                # 승인 버튼이 이 id 자체가 응답에 없어 "undefined"로 호출되던 버그를
                # 고치면서 승인 액션 자체를 여기서 뺐다 — 두 화면에 같은 액션이
                # 따로 있으면 관제사가 어느 쪽이 진짜인지 헷갈린다).
                "assignment_id": row["assignment_id"],
                "status": row["status"],
                "call_sign": row["call_sign"],
                "vessel_name": row["vessel_name"],
                "cargo_chem_id": row["cargo_chem_id"],
                "cargo_name": row["cargo_name"],
                "window_start": row["window_start"],
                "window_end": row["window_end"],
                # 실제 입출항 시각(portmis_vessel 우선, 없으면 upa_port_call) — window_*는
                # 배정 시점의 "계획값"이고 이건 "진짜 언제 들어오고 나갔나"다.
                # 아직 출항 전이면 actual_departure_utc는 null.
                "actual_arrival_utc": row["actual_arrival_utc"],
                "actual_departure_utc": row["actual_departure_utc"],
                # PORT-MIS가 입항 시점에 신고받은 출항 "예정" 시각 — 아직 출항 전인
                # 배정도 "언제쯤 이 자리가 빌지" 미리 보여줄 수 있다. PORT-MIS만 주는
                # 값이라 없으면 그냥 null(다른 소스로 대체하지 않음 — 추측 금지).
                "departure_scheduled_utc": row["departure_scheduled_utc"],
                "assignment_reason": row["assignment_reason"],
                "approved_by": row["approved_by"],
                # 구조화된 배정 근거(선정 경로/흘수여유/안전판정/기상판정/탈락후보) —
                # assignment_reason(LLM 자유문)이 특정 요인만 강조해도 전체 근거를
                # 여기서 확인할 수 있다(OrchestratorResult.decision_detail() 참고).
                "decision_detail": row["decision_detail"],
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
