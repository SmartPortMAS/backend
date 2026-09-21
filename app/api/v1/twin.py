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
  흘수 — 지금은 mart.berth_draught_check 판정을 그대로 옮긴다. 앞으로는 같은 식
         (가용수심 = 표 수심 + 조위, UKC 가 흘수의 10% 이상이면 OK)에 조위 예측만 넣는다.

조위 예측 (2026-09-21 연결)
  국립해양조사원 조석예보(고·저조, 울산 DT_0020 — tide_obs 실측과 같은 관측소)를 받아
  고조·저조 사이를 코사인으로 잇는다(물때표를 시간별로 읽는 통상 방법).
  예보는 달·해에 의한 조석만 계산해 실측과 체계적으로 차이 난다 — 9/19~21 실측 대조
  11점에서 실측이 +16~+27 cm(평균 +22) 높았다. 그래서 최근 72시간 실측과의 차이
  (중앙값)를 더해 쓰고, 그 보정값을 응답에 그대로 적는다. 바람·기압에 의한 앞으로의
  변동은 예측할 수 없다 — 화면은 "예보 기준 전망"으로 표기한다.
"""

import math
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.weather.data_access import ULSAN_PORT_NX, ULSAN_PORT_NY, get_berth_threshold
from app.agents.weather.rule_engine import evaluate
from app.agents.weather.schemas import WorkStatus
from app.config import get_settings
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

# 흘수 판정 → 판정 어휘. mart.berth_draught_check 의 draught_verdict 와 같은 값을 쓴다.
_LEVEL_OF_DRAUGHT = {"OK": "적합", "MARGINAL": "주의", "CHECK": "확인요청", "NOT_ALLOWED": "부적합"}
_DRAUGHT_TXT = {"OK": "여유 충분", "MARGINAL": "여유 부족", "CHECK": "접안 선석 확인 요청",
                "NOT_ALLOWED": "접안 불가"}
_RANK = {"적합": 0, "주의": 1, "확인요청": 2, "판정불가": 2, "부적합": 3}
_GATE_OF_LEVEL = {"적합": "OPEN", "주의": "CAUTION", "확인요청": "CAUTION", "판정불가": "CAUTION",
                  "부적합": "LOCKED"}
_UKC_RATIO = 0.10     # mart.berth_draught_check 와 같은 기준(흘수의 10%)

_focus: dict = {"seq": 0, "berth": None}

# ── 조위 예측 ────────────────────────────────────────────────────────────────
_TIDE_URL = "https://apis.data.go.kr/1192136/tideFcstHghLw/GetTideFcstHghLwApiService"
_TIDE_STATION = "DT_0020"          # 울산 — tide_obs 실측과 같은 관측소
_KST = timezone(timedelta(hours=9))
_TIDE_CACHE_TTL = timedelta(hours=12)   # 천문조 예보라 하루 안에 바뀌지 않는다
_tide_cache: dict[str, tuple[datetime, list]] = {}


async def _tide_extremes_for(day_kst) -> list[tuple[datetime, float]]:
    """그날(KST)의 고·저조 [(UTC 시각, cm)]. 키가 없거나 실패하면 빈 목록."""
    key = get_settings().khoa_tide_fcst_key
    if not key:
        return []
    ds = day_kst.strftime("%Y%m%d")
    now = datetime.now(timezone.utc)
    hit = _tide_cache.get(ds)
    if hit and now - hit[0] < _TIDE_CACHE_TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(_TIDE_URL, params={
            "serviceKey": key, "type": "json", "obsCode": _TIDE_STATION,
            "reqDate": ds, "numOfRows": 20, "pageNo": 1,
        })
    resp.raise_for_status()
    items = (((resp.json().get("body") or {}).get("items") or {}).get("item")) or []
    if isinstance(items, dict):
        items = [items]
    out = sorted(
        (datetime.strptime(i["predcDt"], "%Y-%m-%d %H:%M").replace(tzinfo=_KST).astimezone(timezone.utc),
         float(i["predcTdlvVl"]))
        for i in items
    )
    _tide_cache[ds] = (now, out)
    return out


def _tide_at(extremes: list[tuple[datetime, float]], t: datetime) -> float | None:
    """고·저조 사이를 코사인으로 잇는다 — 극치 시각에서 정확히 그 값, 사이는 반주기 곡선."""
    for (t1, h1), (t2, h2) in zip(extremes, extremes[1:]):
        if t1 <= t <= t2 and t2 > t1:
            f = (t - t1).total_seconds() / (t2 - t1).total_seconds()
            return (h1 + h2) / 2 + (h1 - h2) / 2 * math.cos(math.pi * f)
    return None


def _draught_verdict(depth: float, depth_max: float | None, tide_m: float, draught: float):
    """mart.berth_draught_check 와 같은 판정(CHECK 포함). 반환 (verdict, ukc_m)."""
    avail = depth + tide_m
    ukc = avail - draught
    required = draught * _UKC_RATIO
    if depth_max and depth_max > depth and ukc < required and (depth_max + tide_m - draught) >= required:
        return "CHECK", ukc
    if avail <= draught:
        return "NOT_ALLOWED", ukc
    if ukc < required:
        return "MARGINAL", ukc
    return "OK", ukc


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

_Q_TIDE_OBS = text("""
    SELECT observed_at_utc, tide_level_cm FROM tide_obs
    WHERE observed_at_utc >= :start AND tide_level_cm IS NOT NULL
    ORDER BY observed_at_utc
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
    draught = presence = None
    if call_sign:
        d = (await db.execute(_Q_DRAUGHT, {"cs": call_sign})).mappings().first()
        draught = {k: _iso(v) for k, v in d.items()} if d else None
        p = (await db.execute(_Q_PRESENCE, {"cs": call_sign})).mappings().first()
        presence = {k: _iso(v) for k, v in p.items()} if p else None
    # 흘수 판정에 쓸 값 — 흘수 0·결측이면 흘수 축은 빼고 기상만 본다
    depth = (draught or {}).get("chart_depth_m")
    depth_max = (draught or {}).get("chart_depth_max_m")
    vessel_draught = (draught or {}).get("vessel_draught_m")
    judge_draught = bool(depth) and bool(vessel_draught) and float(vessel_draught) > 0

    w_level, _ = _LEVEL_OF_STATUS[now_status]
    level, headline = w_level, (now_status.value if now_status is not WorkStatus.NORMAL else "정상")
    if judge_draught and draught.get("draught_verdict") in _LEVEL_OF_DRAUGHT:
        d_level = _LEVEL_OF_DRAUGHT[draught["draught_verdict"]]
        if _RANK[d_level] > _RANK[level]:
            level = d_level
        if d_level != "적합":
            headline = (f"{headline} · 흘수 {_DRAUGHT_TXT[draught['draught_verdict']]}"
                        if headline != "정상" else f"흘수 {_DRAUGHT_TXT[draught['draught_verdict']]}")
    current = {
        "status": now_status.value, "level": level, "gate": _GATE_OF_LEVEL[level],
        "headline": headline, "weather_level": w_level, "reasons": now_reasons,
        "wind_ms": wx.get("wind_speed_ms"), "wind_observed_at_utc": _iso(wind_at),
        "wave_m": wx.get("wave_height_sig_m"), "wave_observed_at_utc": _iso(wave_at),
        "tide_m": (wx["tide_level_cm"] / 100.0) if wx.get("tide_level_cm") is not None else None,
        "tide_observed_at_utc": _iso(wx.get("tide_observed_at_utc")),
    }

    # 조위 예측 — 오늘 앞뒤 며칠의 고·저조를 받아 잇고, 최근 72시간 실측과의 차이를 보정한다
    tide_forecast, extremes, bias_cm = None, [], None
    try:
        today = now.astimezone(_KST).date()
        for off in range(-3, 5):
            extremes += await _tide_extremes_for(today + timedelta(days=off))
        extremes.sort()
    except Exception as e:   # noqa: BLE001 — 조위 예측이 없어도 기상 전망은 보여준다
        extremes, tide_forecast = [], {"error": f"조석예보 조회 실패 — {e}"}
    if extremes:
        obs = (await db.execute(_Q_TIDE_OBS, {"start": now - timedelta(hours=72)})).mappings().all()
        resid = [o["tide_level_cm"] - p for o in obs
                 if (p := _tide_at(extremes, o["observed_at_utc"])) is not None]
        bias_cm = round(statistics.median(resid), 1) if resid else 0.0
        tide_forecast = {
            "source": "국립해양조사원 조석예보(고·저조) 울산 DT_0020 — 극치 사이 코사인 보간",
            "bias_cm": bias_cm,
            "bias_basis": f"최근 72시간 실측 {len(resid)}점과 예보의 차이 중앙값",
            "note": "천문조만 예보 — 바람·기압에 의한 앞으로의 변동은 반영 못 함",
            "extremes": [{"at_utc": _iso(t), "tide_cm": h, "tide_m": round((h + (bias_cm or 0)) / 100, 2)}
                         for t, h in extremes if now <= t <= now + timedelta(hours=hours)],
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
        w_lv, _ = _LEVEL_OF_STATUS[st]
        point = {
            "at_utc": _iso(r["fcst_at_utc"]), "issued_at_utc": _iso(r["base_at_utc"]),
            "wind_ms": r["wind_speed_ms"], "wave_m": r["wave_height_m"], "precip_mm": r["precip_mm"],
            "status": st.value, "weather_level": w_lv, "reasons": reasons,
            "tide_m": None, "ukc_m": None, "draught_verdict": None,
        }
        lv = w_lv
        headline = st.value if st is not WorkStatus.NORMAL else "정상"
        pred = _tide_at(extremes, r["fcst_at_utc"]) if extremes else None
        if pred is not None:
            point["tide_m"] = round((pred + (bias_cm or 0)) / 100, 2)
            if judge_draught:
                verdict, ukc = _draught_verdict(float(depth), float(depth_max) if depth_max else None,
                                                point["tide_m"], float(vessel_draught))
                point["ukc_m"], point["draught_verdict"] = round(ukc, 2), verdict
                d_lv = _LEVEL_OF_DRAUGHT[verdict]
                if _RANK[d_lv] > _RANK[lv]:
                    lv = d_lv
                if verdict != "OK":
                    headline = (f"{headline} · 흘수 {_DRAUGHT_TXT[verdict]}" if headline != "정상"
                                else f"흘수 {_DRAUGHT_TXT[verdict]}")
        point.update({"level": lv, "gate": _GATE_OF_LEVEL[lv], "headline": headline})
        points.append(point)
        if first_change is None and lv != "적합":
            first_change = point

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
        "tide_forecast": tide_forecast,
        "forecast_grid": {"nx": grid[0], "ny": grid[1], "note": grid_note},
        "sources": {
            "weather_rule": "weather 에이전트 rule_engine.evaluate (부두그룹 임계값)",
            "forecast": f"기상청 단기예보 — {grid_note} · 같은 예보 시각은 최신 발표값",
            "observation": "mart.weather_now (3시간 넘은 관측은 판단불가)",
            "draught": "mart.berth_draught_check (조위 반영 가용수심)",
        },
    }
