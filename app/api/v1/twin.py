"""3D 정밀 검토(Omniverse) 연동 — 관제사가 지목한 선석과, 그 선석의 앞으로 72시간.

두 트윈의 역할 (2026-09-21 정리)
  3D 관제 화면(React Three.js) — 평소 틀어 두는 상시 관제. "지금 어디에 무엇이"
  정밀 검토(Omniverse)          — 관제사가 지목한 선석이 앞으로 어떻게 바뀌는지를
                                  기상 예보로 재생하고, 재생 데이터가 있는 배는
                                  실제로 있었던 날을 다시 보여준다

왜 백엔드가 중간에 끼나
  Omniverse 화면은 WebRTC 로 영상만 브라우저에 보낸다. 브라우저가 Omniverse 앱에
  "이 배를 보여 달라"고 말할 길이 없어서, 브라우저가 여기에 지목을 적고 Omniverse
  앱(D:\\omniverse\\open_scene.py)이 몇 초마다 읽는다. 지목은 시연 중 잠깐 쓰는
  값이라 프로세스 메모리에만 둔다 — 재시작하면 사라지고 Omniverse 는 순환으로 돌아간다.
  uvicorn 워커 1개 전제(dashboard.py TTL 캐시와 같다).

판정 규칙은 여기서 새로 만들지 않는다
  기상 — weather 에이전트의 rule_engine.evaluate 를 그대로 부른다(부두그룹 임계값·
         강수량 포함). 대시보드와 Omniverse 화면이 같은 시각에 다른 판정을 말하면
         안 된다.
  흘수 — mart.berth_draught_check 가 이미 조위 반영 여유와 판정을 들고 있다.
  조위 예측(국립해양조사원 조석예보)은 아직 연결 전이다. 그 전까지 흘수 여유는
  "지금 실측" 한 값만 내보내고, 앞으로의 변화는 기상 예보만으로 보여준다.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.weather.data_access import ULSAN_PORT_NX, ULSAN_PORT_NY, get_berth_threshold
from app.agents.weather.rule_engine import evaluate
from app.agents.weather.schemas import WorkStatus
from app.core.deps import get_session
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/twin", tags=["twin"])

_OBS_MAX_AGE = timedelta(hours=3)   # weather 에이전트 MAX_STALENESS 와 같다

# 예보 격자 — 온산 부두 11곳이 면한 바다 격자 (103, 82).
#   weather 에이전트가 쓰는 (102, 84)(data_access.ULSAN_PORT_NX/NY)는 이름과 달리 울산시청
#   격자라 육지로 분류돼 파고가 늘 0 이고 풍속도 시내 값이다(2026-09-21 실측·기상청 격자
#   변환식 검산, data-pipeline weather_forecast_collector.FORECAST_GRIDS 주석). 정밀 검토는
#   "이 부두가 앞으로 어떻게 되나"를 보여주는 화면이라 부두 격자를 쓴다. 온산 격자 예보가
#   아직 없으면(수집기 반영 전) 옛 격자로 돌리고 그 사실을 응답에 적는다.
_ONSAN_GRID = (103, 82)

# 기상 단계 → 판정 어휘 · 게이트. 기상 하역중단부터는 "지금 하역하면 안 되는 상태"
# (WorkStatus 주석)라 게이트를 잠근다. 모르면(판단불가) 열지도 잠그지도 않고 주의.
_LEVEL_OF_STATUS = {
    WorkStatus.NORMAL: ("적합", "OPEN"),
    WorkStatus.STOP: ("부적합", "LOCKED"),
    WorkStatus.UNBERTH: ("부적합", "LOCKED"),
    WorkStatus.DISCONNECT: ("부적합", "LOCKED"),
    WorkStatus.UNKNOWN: ("판정불가", "CAUTION"),
}

_focus: dict = {"seq": 0, "berth": None}


class FocusRequest(BaseModel):
    """berth 와 call_sign 이 둘 다 비면 지목을 해제한다(Omniverse 는 순환으로 돌아간다)."""

    berth: str | None = None
    call_sign: str | None = None
    vessel_name: str | None = None


_Q_RESOLVE_BERTH = text("""
    -- 화면마다 선석 표기가 다르다('OTK 1부두' · 'OTK1부두' · 'SK2부두 02').
    -- facility_alias 와 같은 정규화 규칙(mart.norm_berth)으로 마스터 표기를 찾는다.
    SELECT wharf_name FROM upa_berth_facility
    WHERE mart.norm_berth(wharf_name) = mart.norm_berth(:name)
    LIMIT 1
""")

_CYPHER_BERTH_GROUP = """
MATCH (b:Berth {wharf_name: $name})
RETURN b.berth_group AS berth_group LIMIT 1
"""

_Q_WEATHER_NOW = text("""
    SELECT wind_speed_ms, weather_observed_at_utc, wave_height_sig_m, wave_observed_at_utc,
           tide_level_cm, tide_observed_at_utc
    FROM mart.weather_now
""")

_Q_FORECAST = text("""
    -- 같은 예보 시각이 발표마다 한 행씩 쌓인다. 가장 최근 발표값 하나만 쓴다.
    SELECT DISTINCT ON (fcst_at_utc)
           fcst_at_utc, base_at_utc, wind_speed_ms, wave_height_m, precip_mm
    FROM weather_forecast
    WHERE nx = :nx AND ny = :ny
      AND fcst_at_utc >= :start AND fcst_at_utc <= :end
    ORDER BY fcst_at_utc, base_at_utc DESC
""")

_Q_DRAUGHT = text("""
    SELECT facility_name, chart_depth_m, chart_depth_max_m, tide_level_m, vessel_draught_m,
           ukc_m, ukc_required_m, draught_verdict, draught_observed_at_utc
    FROM mart.berth_draught_check
    WHERE callsgn = upper(btrim(:cs))
    LIMIT 1
""")

_Q_PRESENCE = text("""
    SELECT presence_zone, berth_name, berth_basis, vessel_name, received_at_utc
    FROM mart.vessel_presence
    WHERE callsgn = upper(btrim(:cs))
    LIMIT 1
""")


async def _resolve_berth(db: AsyncSession, name: str | None) -> str | None:
    if not name:
        return None
    return (await db.execute(_Q_RESOLVE_BERTH, {"name": name})).scalar()


def _iso(v):
    # numeric 컬럼(round(...)::numeric)은 Decimal 로 오고 JSON 에선 문자열이 된다 —
    # Omniverse 쪽 계산이 숫자를 기대하므로 여기서 float 로 맞춘다.
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


@router.post("/focus", summary="Omniverse 정밀 검토에 보여줄 선석·선박 지목")
async def set_focus(req: FocusRequest, db: AsyncSession = Depends(get_session)) -> dict:
    """브라우저(3D 관제 화면)가 부른다. seq 가 1씩 늘어 Omniverse 가 새 지목을 알아챈다."""
    seq = _focus["seq"] + 1
    if not req.berth and not req.call_sign:
        _focus.clear()
        _focus.update({"seq": seq, "berth": None})
        return dict(_focus)
    berth = await _resolve_berth(db, req.berth)
    if req.berth and berth is None:
        raise HTTPException(status_code=404, detail=f"선석을 찾을 수 없습니다: {req.berth}")
    _focus.clear()
    _focus.update({
        "seq": seq,
        "berth": berth,
        "call_sign": (req.call_sign or "").strip().upper() or None,
        "vessel_name": req.vessel_name,
        "requested_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    return dict(_focus)


@router.get("/focus", summary="현재 지목 (Omniverse 앱이 읽는다)")
async def get_focus() -> dict:
    return dict(_focus)


@router.get("/outlook", summary="선석의 지금 판정과 앞으로의 기상 판정 흐름")
async def get_outlook(
    berth: str = Query(..., description="선석명 — 표기 달라도 됨('OTK 1부두' 등)"),
    call_sign: str | None = Query(None, description="있으면 그 배의 흘수 여유·현재 위치를 같이 준다"),
    hours: int = Query(72, ge=1, le=96),
    db: AsyncSession = Depends(get_session),
) -> dict:
    wharf = await _resolve_berth(db, berth)
    if wharf is None:
        raise HTTPException(status_code=404, detail=f"선석을 찾을 수 없습니다: {berth}")

    async with neo4j_client.driver.session() as session:
        async def _tx(tx):
            rec = await (await tx.run(_CYPHER_BERTH_GROUP, name=wharf)).single()
            return rec["berth_group"] if rec else None

        group = await session.execute_read(_tx)
    threshold = await get_berth_threshold(db, berth_group=group)

    now = datetime.now(timezone.utc)
    wx = (await db.execute(_Q_WEATHER_NOW)).mappings().first() or {}
    wind_at, wave_at = wx.get("weather_observed_at_utc"), wx.get("wave_observed_at_utc")
    now_status, now_reasons = evaluate(
        wind_speed_ms=wx.get("wind_speed_ms"),
        wind_is_stale=wind_at is None or now - wind_at > _OBS_MAX_AGE,
        wave_height_m=wx.get("wave_height_sig_m"),
        wave_is_stale=wave_at is None or now - wave_at > _OBS_MAX_AGE,
        threshold=threshold,
    )
    level, gate = _LEVEL_OF_STATUS[now_status]
    current = {
        "status": now_status.value, "level": level, "gate": gate, "reasons": now_reasons,
        "wind_ms": wx.get("wind_speed_ms"), "wind_observed_at_utc": _iso(wind_at),
        "wave_m": wx.get("wave_height_sig_m"), "wave_observed_at_utc": _iso(wave_at),
        "tide_m": (wx["tide_level_cm"] / 100.0) if wx.get("tide_level_cm") is not None else None,
        "tide_observed_at_utc": _iso(wx.get("tide_observed_at_utc")),
    }

    window = {"start": now, "end": now + timedelta(hours=hours)}
    grid = _ONSAN_GRID
    rows = (await db.execute(_Q_FORECAST, {"nx": grid[0], "ny": grid[1], **window})).mappings().all()
    grid_note = "온산항 부두 바다 격자 (103,82)"
    if not rows:
        grid = (ULSAN_PORT_NX, ULSAN_PORT_NY)
        rows = (await db.execute(_Q_FORECAST, {"nx": grid[0], "ny": grid[1], **window})).mappings().all()
        grid_note = ("울산 시내 격자 (102,84) — 온산 격자 예보가 아직 없어 대신 씀. "
                     "이 격자는 육지라 파고가 0 으로 오므로 파고 판정은 믿지 말 것")
    points, first_change = [], None
    for r in rows:
        st, reasons = evaluate(
            wind_speed_ms=r["wind_speed_ms"], wind_is_stale=False,
            wave_height_m=r["wave_height_m"], wave_is_stale=False,
            threshold=threshold, precip_mm=r["precip_mm"],
        )
        lv, gt = _LEVEL_OF_STATUS[st]
        point = {
            "at_utc": _iso(r["fcst_at_utc"]), "issued_at_utc": _iso(r["base_at_utc"]),
            "wind_ms": r["wind_speed_ms"], "wave_m": r["wave_height_m"], "precip_mm": r["precip_mm"],
            "status": st.value, "level": lv, "gate": gt, "reasons": reasons,
        }
        points.append(point)
        if first_change is None and st is not WorkStatus.NORMAL:
            first_change = point

    draught = presence = None
    if call_sign:
        d = (await db.execute(_Q_DRAUGHT, {"cs": call_sign})).mappings().first()
        draught = {k: _iso(v) for k, v in d.items()} if d else None
        p = (await db.execute(_Q_PRESENCE, {"cs": call_sign})).mappings().first()
        presence = {k: _iso(v) for k, v in p.items()} if p else None

    return {
        "berth": wharf,
        "berth_group": group,
        "thresholds": None if threshold is None else {
            "stop_wind_ms": threshold.stop_wind_ms, "stop_wave_m": threshold.stop_wave_m,
            "unberth_wind_ms": threshold.unberth_wind_ms, "unberth_wave_m": threshold.unberth_wave_m,
            "disconnect_wind_ms": threshold.disconnect_wind_ms, "disconnect_wave_m": threshold.disconnect_wave_m,
        },
        "current": current,
        "forecast": points,
        "first_change": first_change,
        "draught": draught,
        "presence": presence,
        "tide_forecast": None,   # 조석예보 연결 전 — 흘수 여유는 현재 실측 한 값
        "forecast_grid": {"nx": grid[0], "ny": grid[1], "note": grid_note},
        "sources": {
            "weather_rule": "weather 에이전트 rule_engine.evaluate (부두그룹 임계값)",
            "forecast": f"기상청 단기예보 — {grid_note} · 같은 예보 시각은 최신 발표값",
            "observation": "mart.weather_now (3시간 넘은 관측은 판단불가)",
            "draught": "mart.berth_draught_check (조위 반영 가용수심)",
        },
    }
