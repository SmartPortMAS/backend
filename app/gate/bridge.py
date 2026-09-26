"""하역 개시 인터락 게이트(라즈베리파이) ↔ 화면 다리 — MQTT 와 WebSocket 사이.

계약: 하드웨어/UI연동_전달사항_20260922.md (동안님 9/21 동의). 토픽은 gate/{id}/… 이고
7월 mockHardware.js 의 smartport/gate/… 계약은 폐기됐다.

  백엔드 → 파이   gate/{id}/interlock  {"state":"LOCKED","reason":"WIND 16m/s>=14","reason_ko":"…"}  retain
                  gate/{id}/cmd        {"action":"OPEN"|"CLOSE"}
  파이 → 백엔드   gate/{id}/status     5초마다 + 변화 즉시, retain

역할을 나누는 선 — 거부는 장치가 한다
  화면에서 "하역 개시 요청"을 누르면 여기서는 cmd 를 발행만 한다. 잠긴 상태에서 열어도
  되는지는 파이가 판단해 status.denied 로 돌려준다. 여기서 먼저 거부하면 "화면이 막았다"가
  되고, 하드웨어 페일세이프를 보여주는 시연 장면 ③이 성립하지 않는다.

잠그는 근거는 둘이고, 둘 다 새로 만들지 않는다
  ① 기상 — weather 에이전트 rule_engine.evaluate(부두군 임계값)를 3초마다 그대로 부른다.
     정상이 아니면(하역중단 이상·판단불가) 잠근다.
  ② 판정 — 지금 그 선석에 붙어 있는 배(mart.vessel_presence)의 최신 '하역중' 판정이
     부적합·판정불가면 잠근다. services/gate_state.py(/ws/hardware)와 같은 규칙이다
     (models/assessment_history.GATE_BLOCKING_LEVELS). 판정 이력 표가 없는 DB면 ①만 쓴다.
     · 판정 이력은 값이 바뀔 때만 기록되므로 시간으로 자르지 않는다. 대신 "지금 붙어
       있는 배"의 "이번 입항 뒤" 판정만 본다 — 떠난 배의 판정으로 잠그지 않고, 오래
       그대로인 부적합을 놓치지도 않는다.
     · 붙어 있는 배가 없으면 ①만 적용하고 화면에 그렇게 쓴다.
  둘 중 하나라도 잠그면 LOCKED. 상태가 바뀔 때만 발행하고, 발행하지 않아도 60초마다
  한 번은 다시 보내 파이가 재부팅돼도 최신 판정을 갖게 한다.

시연 입력
  시연장에서 실제 풍속이 16 m/s 가 될 리 없어, 풍속·파고를 주입하는 입력을 둔다.
  주입값은 실제 판정 코드가 그대로 읽는다(규칙은 같고 값만 다르다). 주입 중이면 응답에
  demo 가 실려 화면이 "시연 입력 중" 배지를 띄운다 — 심사 질문에 정직하게 답하기 위해서다.

브로커가 없어도 앱은 뜬다 — 게이트 카드가 "연결 끊김"으로 보일 뿐이다.
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt
from sqlalchemy import text

from app.agents.weather.data_access import get_berth_threshold
from app.agents.weather.rule_engine import evaluate
from app.agents.weather.schemas import WorkStatus
from app.config import get_settings
from app.database import AsyncSessionFactory
from app.models.assessment_history import GATE_BLOCKING_LEVELS, AssessmentStage
from app.neo4j_client import neo4j_client

logger = logging.getLogger("gate_bridge")

OFFLINE_AFTER_SEC = 15.0      # 파이는 5초마다 status 를 보낸다 — 세 번 놓치면 끊김
EVAL_PERIOD_SEC = 3.0         # 판정 주기 (weather_now 한 줄 조회라 가볍다)
REPUBLISH_SEC = 60.0          # 바뀌지 않아도 이 간격으로 interlock 을 다시 발행
_OBS_MAX_AGE = timedelta(hours=3)   # weather 에이전트 MAX_STALENESS 와 같다
ASSESS_PERIOD_SEC = 15.0      # 판정 이력은 감시 작업이 10분마다 쓴다 — 15초면 충분하다
_BLOCKING = tuple(level.value for level in GATE_BLOCKING_LEVELS)   # 부적합 · 판정불가

_Q_HAS_ASSESSMENT = text("SELECT to_regclass('public.assessment_history') IS NOT NULL")
# 게이트 선석에 지금 붙어 있는 배와, 그 배의 이번 입항 뒤 최신 '하역중' 판정.
# 입항 시각을 모르면 3일로 자른다(지난 입항의 판정을 끌어오지 않게).
_Q_BERTH_ASSESSMENT = text("""
    WITH here AS (
        SELECT upper(btrim(callsgn)) AS cs, vessel_name, berth_name, vts_arrival_at_utc
        FROM mart.vessel_presence
        WHERE presence_zone = 'BERTH'
          AND berth_name = ANY(CAST(:berths AS text[]))
          AND nullif(btrim(callsgn), '') IS NOT NULL
    )
    SELECT h.berth_name, h.cs AS call_sign, coalesce(a.vessel_name, h.vessel_name) AS vessel_name,
           a.level, a.reasons, a.wharf_name AS assessed_wharf, a.assessed_at_utc
    FROM here h
    LEFT JOIN LATERAL (
        SELECT ah.level, ah.reasons, ah.wharf_name, ah.vessel_name, ah.assessed_at_utc
        FROM assessment_history ah
        WHERE upper(btrim(ah.call_sign)) = h.cs
          AND ah.stage = :during_cargo
          AND ah.assessed_at_utc >= coalesce(h.vts_arrival_at_utc, now() - interval '3 days')
        ORDER BY ah.assessed_at_utc DESC
        LIMIT 1
    ) a ON true
    ORDER BY h.berth_name, h.cs
""")

_Q_WEATHER_NOW = text("""
    SELECT wind_speed_ms, weather_observed_at_utc, wave_height_sig_m, wave_observed_at_utc
    FROM mart.weather_now
""")
_CYPHER_BERTH_GROUP = "MATCH (b:Berth {wharf_name: $name}) RETURN b.berth_group AS g LIMIT 1"


def _short_reason(status: WorkStatus, wind, wave, th) -> str:
    """LCD 둘째 줄용 — 영문·숫자 16자 이내. 파이가 그대로 띄운다."""
    if status is WorkStatus.UNKNOWN:
        return "NO WEATHER DATA"
    if th is not None:
        if wind is not None and th.stop_wind_ms is not None and wind >= th.stop_wind_ms:
            return f"WIND {wind:g}m/s>={th.stop_wind_ms:g}"[:16]
        if wave is not None and th.stop_wave_m is not None and wave >= th.stop_wave_m:
            return f"WAVE {wave:g}m>={th.stop_wave_m:g}"[:16]
    return status.name[:16]


class GateBridge:
    def __init__(self) -> None:
        s = get_settings()
        self.host, self.port = s.mqtt_host, s.mqtt_port
        # 게이트 → 선석(마스터 표기). 시연 장치는 G01 = 선석 A, G02 = 선석 B.
        self.berth_of: dict[str, str] = json.loads(s.gate_berths)
        self._lock = threading.Lock()
        self._status: dict[str, tuple[dict, float]] = {}      # gate_id -> (파이 status, 받은 시각 monotonic)
        self._interlock: dict[str, dict] = {}                 # gate_id -> 마지막 발행
        self._demo: dict = {}                                 # {"wind_ms", "wave_m", "set_at_utc"}
        # 시연 판정 — 게이트별 {"level", "reason", "set_at_utc"}. 판정 이력(assessment_history)에는
        # 쓰지 않는다: 운영 DB 의 판정 기록·확인 대기 목록을 시연값으로 오염시키지 않기 위해서다.
        self._demo_verdict: dict[str, dict] = {}
        self._groups: dict[str, str | None] = {}              # gate_id -> berth_group (Neo4j, 한 번만)
        self._thresholds: dict[str, dict] = {}
        self._assess: dict[str, list[dict]] = {}              # 선석 -> 붙어 있는 배와 최신 하역중 판정
        self._assess_mono = 0.0
        self._assess_connected: bool | None = None             # None = 아직 안 봄, False = 표 없음
        self.connected = False
        self.client: mqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None

    # ── MQTT ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="safeberth-backend")
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.connect_async(self.host, self.port, keepalive=30)   # 브로커가 없으면 백그라운드에서 재시도
        c.loop_start()
        self.client = c
        self._task = asyncio.create_task(self._evaluate_loop(), name="gate-evaluate")
        logger.info("게이트 다리 시작 — 브로커 %s:%s · 게이트 %s", self.host, self.port, self.berth_of)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        if getattr(reason_code, "is_failure", False) or (isinstance(reason_code, int) and reason_code != 0):
            self.connected = False
            self._last_error = f"브로커 연결 거부 ({reason_code})"
            return
        self.connected = True
        self._last_error = None
        client.subscribe("gate/+/status")
        # 재연결 직후 파이가 최신 판정을 갖도록 마지막 발행분을 다시 보낸다
        with self._lock:
            sent = dict(self._interlock)
        for gate_id, il in sent.items():
            self._publish(f"gate/{gate_id}/interlock", il["payload"], retain=True)
        logger.info("브로커 연결됨 — gate/+/status 구독")

    def _on_disconnect(self, _client, _userdata, _flags=None, reason_code=None, _properties=None):
        self.connected = False
        logger.warning("브로커 연결 끊김 (%s) — 재시도", reason_code)

    def _on_message(self, _client, _userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 3 or parts[2] != "status":
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:  # noqa: BLE001 — 깨진 메시지 하나로 죽지 않는다
            logger.warning("status 해석 실패: %r", msg.payload[:80])
            return
        with self._lock:
            self._status[parts[1]] = (payload, time.monotonic())

    def _publish(self, topic: str, payload: dict, *, retain: bool) -> bool:
        if not self.client:
            return False
        info = self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=retain)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def publish_cmd(self, gate_id: str, action: str) -> bool:
        """화면의 요청을 그대로 장치에 넘긴다. 거부 여부는 여기서 정하지 않는다."""
        return self._publish(f"gate/{gate_id}/cmd", {"action": action}, retain=False)

    # ── 시연 입력 ─────────────────────────────────────────────────────────
    def set_demo(self, wind_ms: float | None, wave_m: float | None) -> None:
        with self._lock:
            if wind_ms is None and wave_m is None:
                self._demo = {}
            else:
                self._demo = {"wind_ms": wind_ms, "wave_m": wave_m,
                              "set_at_utc": datetime.now(timezone.utc).isoformat()}

    def set_demo_verdict(self, gate_id: str | None, level: str | None, reason: str | None = None) -> None:
        """시연 판정 — 그 게이트 선석의 '하역중 최신 판정'을 이 값으로 본다(규칙은 실제와 같다).

        실제 흐름: 하역 중 판정이 부적합·판정불가로 바뀌면 터미널은 하역을 멈춘다. 시연장에서는
        그 판정 변화를 기다릴 수 없어, 판정 값만 넣고 잠금 규칙(GATE_BLOCKING_LEVELS)은 그대로 쓴다.
        gate_id 가 None 이면 모든 게이트. level 이 None 이면 해제(실측 판정으로 복귀).
        """
        targets = list(self.berth_of) if gate_id is None else [gate_id]
        with self._lock:
            for gid in targets:
                if level is None:
                    self._demo_verdict.pop(gid, None)
                else:
                    self._demo_verdict[gid] = {"level": level, "reason": (reason or "").strip()[:80],
                                               "set_at_utc": datetime.now(timezone.utc).isoformat()}

    # ── 판정 → interlock ──────────────────────────────────────────────────
    async def _berth_group(self, gate_id: str, berth: str) -> str | None:
        if gate_id in self._groups:
            return self._groups[gate_id]
        try:
            async with neo4j_client.driver.session() as session:
                async def _tx(tx):
                    rec = await (await tx.run(_CYPHER_BERTH_GROUP, name=berth)).single()
                    return rec["g"] if rec else None
                self._groups[gate_id] = await session.execute_read(_tx)
        except Exception as e:  # noqa: BLE001
            logger.warning("선석 부두군 조회 실패 %s: %s", berth, e)
            return None
        return self._groups[gate_id]

    async def _refresh_assessment(self, db) -> None:
        """게이트 선석별로 지금 붙어 있는 배와 그 배의 최신 하역중 판정을 읽는다(15초 캐시)."""
        if self._assess_connected is not None and time.monotonic() - self._assess_mono < ASSESS_PERIOD_SEC:
            return
        self._assess_mono = time.monotonic()
        if not (await db.execute(_Q_HAS_ASSESSMENT)).scalar():
            self._assess_connected, self._assess = False, {}
            return
        rows = (await db.execute(_Q_BERTH_ASSESSMENT, {
            "berths": sorted(set(self.berth_of.values())),
            "during_cargo": AssessmentStage.DURING_CARGO.value,
        })).mappings().all()
        by_berth: dict[str, list[dict]] = {}
        for r in rows:
            by_berth.setdefault(r["berth_name"], []).append({
                "call_sign": r["call_sign"],
                "vessel_name": r["vessel_name"],
                "level": r["level"],                     # None = 이번 입항 뒤 하역중 판정 없음
                "reasons": list(r["reasons"] or [])[:3],
                "assessed_wharf": r["assessed_wharf"],
                "assessed_at_utc": r["assessed_at_utc"].isoformat() if r["assessed_at_utc"] else None,
                "blocking": r["level"] in _BLOCKING,
            })
        self._assess_connected, self._assess = True, by_berth

    async def evaluate_once(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            demo = dict(self._demo)
            demo_verdict = {k: dict(v) for k, v in self._demo_verdict.items()}
        async with AsyncSessionFactory() as db:
            wx = (await db.execute(_Q_WEATHER_NOW)).mappings().first() or {}
            try:
                await self._refresh_assessment(db)
            except Exception as e:  # noqa: BLE001 — 판정 이력을 못 읽어도 기상 잠금은 계속한다
                await db.rollback()
                self._assess_connected = None
                logger.warning("판정 이력 조회 실패: %s", e)
            for gate_id, berth in self.berth_of.items():
                group = await self._berth_group(gate_id, berth)
                th = await get_berth_threshold(db, berth_group=group)
                if th is not None:
                    self._thresholds[gate_id] = {
                        "stop_wind_ms": th.stop_wind_ms, "stop_wave_m": th.stop_wave_m,
                        "unberth_wind_ms": th.unberth_wind_ms, "disconnect_wind_ms": th.disconnect_wind_ms,
                    }
                wind_at, wave_at = wx.get("weather_observed_at_utc"), wx.get("wave_observed_at_utc")
                wind = demo["wind_ms"] if demo.get("wind_ms") is not None else wx.get("wind_speed_ms")
                wave = demo["wave_m"] if demo.get("wave_m") is not None else wx.get("wave_height_sig_m")
                wind_stale = demo.get("wind_ms") is None and (wind_at is None or now - wind_at > _OBS_MAX_AGE)
                wave_stale = demo.get("wave_m") is None and (wave_at is None or now - wave_at > _OBS_MAX_AGE)
                status, reasons = evaluate(
                    wind_speed_ms=wind, wind_is_stale=wind_stale,
                    wave_height_m=wave, wave_is_stale=wave_stale, threshold=th,
                )
                weather_lock = status is not WorkStatus.NORMAL
                dv = demo_verdict.get(gate_id)
                if dv:
                    # 시연 판정이 있으면 실측 판정 대신 쓴다. 적합·주의면 판정 잠금 없음.
                    blocking = ([{"vessel_name": "[시연]", "call_sign": "", "level": dv["level"],
                                  "reasons": [dv["reason"]] if dv["reason"] else [], "blocking": True}]
                                if dv["level"] in _BLOCKING else [])
                else:
                    blocking = [v for v in self._assess.get(berth, []) if v["blocking"]]
                state = "LOCKED" if (weather_lock or blocking) else "UNLOCKED"
                reason, reason_ko = "", []
                if weather_lock:
                    reason = _short_reason(status, wind, wave, th)
                    # 화면 근거는 넘긴 항목만 — "파고 0.5m < 2.0m 정상" 같은 줄까지 붙이면 관제사가
                    # 무엇 때문에 잠겼는지 한눈에 못 본다. 넘긴 항목이 없으면(판단불가) 전부 보인다.
                    exceeded = [r for r in reasons if "->" in r]
                    reason_ko.append(" · ".join(exceeded or reasons))
                if blocking:
                    v = blocking[0]
                    reason = reason or ("VERDICT UNFIT" if v["level"] == "부적합" else "VERDICT UNKNOWN")
                    why = v["reasons"][0] if v["reasons"] else ""
                    reason_ko.append(f"{v['vessel_name'] or v['call_sign']} 하역중 판정 {v['level']}"
                                     + (f" — {why}" if why else ""))
                payload = {"state": state}
                if state == "LOCKED":
                    payload["reason"] = reason[:16]
                    payload["reason_ko"] = " / ".join(reason_ko)
                with self._lock:
                    prev = self._interlock.get(gate_id)
                changed = (prev is None or prev["payload"].get("state") != state
                           or prev["payload"].get("reason") != payload.get("reason", "")
                           or prev["payload"].get("reason_ko") != payload.get("reason_ko"))
                due = prev is None or time.monotonic() - prev["sent_mono"] > REPUBLISH_SEC
                if changed or due:
                    ok = self._publish(f"gate/{gate_id}/interlock", payload, retain=True)
                    with self._lock:
                        self._interlock[gate_id] = {
                            "payload": payload, "status": status.value, "reasons": reasons,
                            "lock_by": {"weather": weather_lock, "assessment": bool(blocking)},
                            "wind_ms": wind, "wave_m": wave, "demo": bool(demo) or bool(dv),
                            "demo_verdict": dv,
                            "sent_at_utc": now.isoformat(), "sent_mono": time.monotonic(), "published": ok,
                        }
                    if changed:
                        logger.info("게이트 %s (%s) → %s %s", gate_id, berth, state, reason or "")

    async def _evaluate_loop(self) -> None:
        while True:
            try:
                await self.evaluate_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 한 번 실패로 루프를 멈추지 않는다
                self._last_error = f"판정 실패: {type(e).__name__}"
                logger.warning("게이트 판정 실패: %s", e)
            await asyncio.sleep(EVAL_PERIOD_SEC)

    # ── 화면용 스냅샷 ─────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        now_mono = time.monotonic()
        with self._lock:
            status = dict(self._status)
            interlock = dict(self._interlock)
            demo = dict(self._demo)
            demo_verdict = {k: dict(v) for k, v in self._demo_verdict.items()}
        assess = dict(self._assess)
        gates = []
        for i, (gate_id, berth) in enumerate(self.berth_of.items()):
            st, at = status.get(gate_id, (None, None))
            age = None if at is None else round(now_mono - at, 1)
            il = interlock.get(gate_id)
            gates.append({
                "gate_id": gate_id,
                "label": f"선석 {chr(65 + i)}",
                "berth": berth,
                "berth_group": self._groups.get(gate_id),
                "thresholds": self._thresholds.get(gate_id),
                "interlock": None if il is None else {
                    "state": il["payload"]["state"], "reason": il["payload"].get("reason"),
                    "reason_ko": il["payload"].get("reason_ko"), "status": il["status"],
                    "reasons": il["reasons"], "wind_ms": il["wind_ms"], "wave_m": il["wave_m"],
                    "demo": il["demo"], "sent_at_utc": il["sent_at_utc"],
                    "lock_by": il.get("lock_by"),
                },
                # 지금 이 선석에 붙어 있는 배와 최신 하역중 판정 (없으면 빈 목록 — 기상만 적용)
                "vessels": assess.get(berth, []),
                # 시연 판정 — 있으면 위 실측 판정 대신 잠금 판단에 쓰는 중
                "demo_verdict": demo_verdict.get(gate_id),
                "status": st,                       # 파이가 보낸 그대로 (interlock·valve·denied·last_result…)
                "offline": at is None or (now_mono - at) > OFFLINE_AFTER_SEC,
                "status_age_sec": age,
            })
        return {
            "broker": {"host": self.host, "port": self.port, "connected": self.connected, "error": self._last_error},
            "demo": demo or None,
            "demo_verdict": demo_verdict or None,
            "assessment_connected": self._assess_connected,
            "gates": gates,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }


bridge = GateBridge()
