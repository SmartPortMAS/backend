"""입항 예정 선박 — 읽기 전용 API (SafeBerth 방향 C "입항 전" 단계의 입력).

PORT-MIS 수집기가 오늘~+3일 입항 신고를 함께 모으게 되면서(2026-09-17,
portmis_collector.LOOKAHEAD_DAYS) 아직 입항하지 않은 액체화물선과 그 사전배정
계류시설을 알 수 있다. 이 라우터는 그 목록에 판정에 쓰일 사실만 붙여 돌려준다.

새 판정 로직은 없다 — 판정(적합/주의/부적합/판정불가)은 에이전트가 내리고
assessment_history(판정 이력, D1)에 남긴다. 그 표가 생기면 여기서 최신 판정을
같이 붙이고, 없으면 assessment=None 으로 둔다.

stage(지금 어느 시점인가)는 판정이 아니라 사실이다 — UPA 선박위치 항해상태가
정박(계류)이면 하역 중, 정박(앵커링)이거나 입항 시각이 지났으면 접안 직전,
그 외는 입항 전.

upa_* · mart.* 는 data-pipeline 이 만든 표라 ORM 없이 raw SQL 로 읽는다
(dashboard.py 와 같은 원칙).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session

router = APIRouter(prefix="/arrivals", tags=["dashboard"])

_QUERY_UPCOMING = text("""
    WITH arr AS (
        SELECT pv.callsgn, pv.vessel_name, pv.ship_kind_nm, pv.nationality_nm, pv.gross_tonnage,
               pv.agency_name, pv.prev_port_nm, pv.entry_purpose_nm,
               pv.arrival_at_utc, pv.departure_sched_utc, pv.arrival_report_type,
               pv.arrival_facility_cd, pv.arrival_facility_nm, pv.collected_at_utc,
               pv.entry_year, pv.entry_count,
               CASE WHEN pv.entry_year IS NOT NULL AND pv.entry_count IS NOT NULL
                    THEN upper(btrim(pv.callsgn)) || '_' || pv.entry_year::text || '_' || lpad(pv.entry_count::text, 3, '0')
               END AS port_call_key
        FROM portmis_vessel pv
        WHERE pv.is_liquid_cargo_vessel
          AND pv.arrival_at_utc BETWEEN now() - make_interval(hours => :past_hours)
                                    AND now() + make_interval(hours => :ahead_hours)
    ),
    -- [2026-09-25] 이 입항 건(콜사인·입항연도·입항횟수)에 단 화물 전부. 입항 후
    -- 위치 화면(mart.vessel_current_call 경유)과 같은 키라 같은 화물이 나온다.
    -- [2026-09-30] 행마다 LATERAL 로 cargo_msds 를 다시 훑던 것(100건 × 23ms)을 한 번 집계로 바꿈.
    cg AS (
        SELECT cm.port_call_key,
               json_agg(json_build_object(
                   'name', coalesce(cm.msds_name_ko, cm.cargo_name_raw),
                   'un_no', cm.dg_un_no, 'chem_id', cm.chem_id,
                   'cas_no', cm.cas_no, 'is_synthetic', cm.is_synthetic
               ) ORDER BY (cm.chem_id IS NULL), cm.bl_no) AS cargos
        FROM mart.cargo_msds cm
        WHERE cm.port_call_key IN (SELECT port_call_key FROM arr)
        GROUP BY cm.port_call_key
    ),
    fac AS (
        SELECT DISTINCT ON (source_name) source_name, wharf_name, facility_type
        FROM mart.facility_alias
        ORDER BY source_name, wharf_name NULLS LAST
    ),
    berth AS (
        SELECT wharf_name, min(depth_m) AS depth_m, bool_or(port_name = '온산항') AS is_onsan,
               string_agg(DISTINCT handling_cargo_name, ', ') AS handling_cargo_name
        FROM upa_berth_facility
        GROUP BY wharf_name
    ),
    pos AS (
        SELECT DISTINCT ON (upper(btrim(callsgn))) upper(btrim(callsgn)) AS cs,
               nav_status_code, received_at_utc
        FROM upa_vessel_position
        WHERE received_at_utc > now() - interval '3 hours' AND callsgn IS NOT NULL
        ORDER BY upper(btrim(callsgn)), received_at_utc DESC
    ),
    draught AS (
        SELECT DISTINCT ON (upper(btrim(callsgn))) upper(btrim(callsgn)) AS cs,
               draught, received_at_utc
        FROM upa_vessel_position
        WHERE draught > 0 AND received_at_utc > now() - interval '30 days' AND callsgn IS NOT NULL
        ORDER BY upper(btrim(callsgn)), received_at_utc DESC
    ),
    spec AS (
        SELECT DISTINCT ON (upper(btrim(callsgn))) upper(btrim(callsgn)) AS cs, draught_m
        FROM vessel_spec
        WHERE draught_m > 0 AND callsgn IS NOT NULL
        ORDER BY upper(btrim(callsgn)), collected_at_utc DESC NULLS LAST
    )
    SELECT a.callsgn AS call_sign, a.vessel_name, a.ship_kind_nm AS ship_kind, a.nationality_nm AS nationality,
           a.gross_tonnage, a.agency_name, a.prev_port_nm AS prev_port, a.entry_purpose_nm AS purpose,
           a.arrival_at_utc, a.departure_sched_utc, a.arrival_report_type AS report_type,
           a.arrival_facility_cd AS facility_cd, a.arrival_facility_nm AS facility_name,
           f.facility_type, f.wharf_name, b.depth_m, b.is_onsan, b.handling_cargo_name,
           p.nav_status_code, p.received_at_utc AS position_at_utc,
           COALESCE(d.draught, s.draught_m) AS draught_m,
           CASE WHEN d.draught IS NOT NULL THEN '실측' WHEN s.draught_m IS NOT NULL THEN '제원최대' END AS draught_basis,
           d.received_at_utc AS draught_at_utc,
           -- dev 화면(판정 요청 버튼)이 쓰는 대표 화물 — 이 입항 건 화물의 첫 행. 콜사인으로
           -- 붙이면 다른 항차 화물을 집는다(2026-09-27 실측: 입항 예정 87건 중 46건).
           cg.cargos->0->>'chem_id' AS chem_id, cg.cargos->0->>'cas_no' AS cas_no,
           cg.cargos->0->>'name' AS cargo_name, (cg.cargos->0->>'is_synthetic')::boolean AS cargo_is_synthetic,
           a.collected_at_utc,
           cg.cargos,
           -- [2026-09-29] 이 행의 입항 건 키와 판정 잡이 지금 보는 입항 건 키. 판정 기록은 같은 입항 건 것만 붙인다.
           a.port_call_key,
           vc.port_call_key AS current_call_key
    FROM arr a
    LEFT JOIN fac f ON f.source_name = a.arrival_facility_nm
    LEFT JOIN berth b ON b.wharf_name = f.wharf_name
    LEFT JOIN pos p ON p.cs = upper(btrim(a.callsgn))
    LEFT JOIN draught d ON d.cs = upper(btrim(a.callsgn))
    LEFT JOIN spec s ON s.cs = upper(btrim(a.callsgn))
    LEFT JOIN cg ON cg.port_call_key = a.port_call_key
    -- [2026-09-30] 상관 서브쿼리로 행마다 뷰를 다시 계산하던 것(100건 × 20ms)을 조인으로 바꿈.
    --   vessel_current_call 은 콜사인당 1행(스칼라 서브쿼리였으므로)이라 행 수가 늘지 않는다.
    LEFT JOIN mart.vessel_current_call vc ON vc.callsgn = upper(btrim(a.callsgn))
    ORDER BY a.arrival_at_utc
""")

_QUERY_HAS_HISTORY = text("SELECT to_regclass('public.assessment_history') IS NOT NULL")

# [2026-09-29] 입항 건별 최신 판정. 예전엔 호출부호의 최신 판정 하나를 붙여, 입항이 12시간 넘게 남은
#   배의 새 입항 건 옆에 지난 입항 건 판정이 떴다(실측: 기록이 있는 7척 전부 화물이 달랐다).
#   입항 건 키가 없는 옛 기록(port_call_key '')은 이 행이 판정 잡의 지금 입항 건일 때만 붙인다.
_QUERY_LATEST_ASSESSMENT = text("""
    SELECT DISTINCT ON (upper(btrim(call_sign)), COALESCE(input_snapshot->>'port_call_key', ''))
           upper(btrim(call_sign)) AS cs, COALESCE(input_snapshot->>'port_call_key', '') AS port_call_key,
           call_sign, stage, level, reasons, action, recipient, changed_from, assessed_at_utc
    FROM assessment_history
    WHERE upper(btrim(call_sign)) = ANY(CAST(:call_signs AS text[]))
    ORDER BY upper(btrim(call_sign)), COALESCE(input_snapshot->>'port_call_key', ''), assessed_at_utc DESC
""")


def _assessment_for(row: dict, by_key: dict[tuple[str, str], dict]) -> dict | None:
    """이 행(입항 건)의 판정. 같은 입항 건 기록이 없으면 키 없는 옛 기록을, 지금 입항 건일 때만."""
    cs = (row.get("call_sign") or "").strip().upper()
    key = row.get("port_call_key") or ""
    hit = by_key.get((cs, key)) if key else None
    if hit is None and (not key or key == row.get("current_call_key")):
        hit = by_key.get((cs, ""))
    return hit


def _stage(row: dict, now: datetime) -> str:
    nav = row.get("nav_status_code") or ""
    if nav.startswith("정박(계류)"):
        return "하역중"
    arrival = row.get("arrival_at_utc")
    if nav.startswith("정박(앵커링)") or (arrival is not None and arrival <= now):
        return "접안직전"
    return "입항전"


@router.get("/upcoming", summary="입항 예정 액체화물선 (사전배정 계류시설 포함)")
async def get_upcoming_arrivals(
    ahead_hours: int = Query(72, ge=1, le=168, description="지금부터 몇 시간 뒤 입항까지"),
    past_hours: int = Query(12, ge=0, le=72, description="입항 시각이 지난 선박을 몇 시간 전까지 포함할지"),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """입항 예정 액체화물선 목록.

    - chart_margin_m: 표 수심 - 흘수. **조위 미반영** 참고값이며 판정이 아니다
      (판정은 조위를 더한 가용수심으로 에이전트가 한다).
    - draught_basis: 실측(최근 30일 UPA 선박위치) / 제원최대(선박제원) / None(판정불가 사유).
    - report_type: PORT-MIS 신고구분(최초/변경/최종). 최종이 아니면 arrival_at_utc 는 예정.
    """
    rows = [dict(r) for r in (await db.execute(
        _QUERY_UPCOMING, {"ahead_hours": ahead_hours, "past_hours": past_hours},
    )).mappings().all()]

    assessments: dict[tuple[str, str], dict] = {}
    has_history = bool((await db.execute(_QUERY_HAS_HISTORY)).scalar())
    if rows and has_history:
        call_signs = sorted({r["call_sign"].strip().upper() for r in rows if r["call_sign"]})
        for a in (await db.execute(_QUERY_LATEST_ASSESSMENT, {"call_signs": call_signs})).mappings().all():
            a = dict(a)
            assessments[(a.pop("cs"), a.pop("port_call_key"))] = a

    now = datetime.now(timezone.utc)
    items = []
    for r in rows:
        depth, draught = r.get("depth_m"), r.get("draught_m")
        r["chart_margin_m"] = round(float(depth) - float(draught), 2) if depth is not None and draught is not None else None
        r["stage"] = _stage(r, now)
        r["assessment"] = _assessment_for(r, assessments)
        items.append(r)

    return {
        "count": len(items),
        "onsan_count": sum(1 for r in items if r.get("is_onsan")),
        "berth_count": sum(1 for r in items if r.get("facility_type") == "BERTH"),
        "has_assessment_history": has_history,
        "items": items,
    }
