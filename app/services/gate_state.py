"""하역 개시 인터락 상태 계산 (D5).

게이트는 **부두 진입이 아니라 하역 밸브 앞에 선다**(9/17 회의 B4, 프런트
HardwarePanel.jsx 주석). 그래서 이 게이트를 여는 근거는 "이 배가 들어와도 되나"가
아니라 **"지금 이 자리에서 하역을 시작해도 되나"** 다.

판정 근거는 회의 §8 이 정한 그대로다.

    하역중 단계의 최신 판정이 '부적합' 또는 '판정불가'  →  INTERLOCK

'판정불가'가 여는 쪽이 아니라 닫는 쪽인 게 핵심이다. 근거가 없다는 것은 안전
하다는 뜻이 아니다(회의 §4 "근거 부족을 안전과 구분"). weather/schemas.py 의
`UNKNOWN = "판단불가"` 가 이미 같은 원칙을 쓴다.

[assessment_history 가 아직 없을 때]
  전 게이트를 INTERLOCK 으로 둔다. 같은 원칙의 극단값이다 — 판정 체계 자체가
  연결되지 않았으면 우리는 아무것도 모르는 상태다. 이렇게 두면 하드웨어는
  판정 연결을 기다리지 않고 배선·릴레이 시험을 바로 시작할 수 있다.

[MQTT]
  아직 붙이지 않았다. backend/requirements.txt 에 클라이언트가 없고 브로커
  주소도 정해지지 않았다. 지금은 판정만으로 상태를 만들어 WS 로 내보낸다 —
  장치 없이도 화면과 인터락 로직이 전부 동작한다. 브로커가 정해지면
  `smartport/gate/+/status` 구독을 이 모듈 안에 꽂고 `fail_safe_ok` 를 장치
  실값으로 바꾸면 된다. **WS 계약은 바뀌지 않는다.**
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment_history import GATE_BLOCKING_LEVELS, AssessmentStage

logger = logging.getLogger("gate_state")

# 프런트 mocks/mockHardware.js 의 "★ MQTT 토픽 계약 ★" 과 같은 값이어야 한다.
STATE_OPEN = "OPEN"
STATE_CLOSED = "CLOSED"
STATE_INTERLOCK = "INTERLOCK"

BEACON_GREEN = "GREEN"
BEACON_AMBER = "AMBER"
BEACON_RED = "RED"

MODE_AUTO = "AUTO"
MODE_MANUAL = "MANUAL"

_BLOCKING_VALUES = tuple(level.value for level in GATE_BLOCKING_LEVELS)


_QUERY_TABLE_EXISTS = text("SELECT to_regclass('public.assessment_history') IS NOT NULL")

# 게이트는 계선시설 단위다. 한 시설에 여러 배가 붙어 있으면 **가장 나쁜 판정**이
# 그 시설의 게이트를 정한다 — 한 배가 부적합인데 옆 배가 적합이라고 밸브를 열 수는 없다.
_QUERY_GATE_SOURCE = text("""
    WITH latest AS (
        SELECT DISTINCT ON (call_sign)
               call_sign, vessel_name, wharf_name, level, stage, reasons, assessed_at_utc
        FROM assessment_history
        WHERE stage = :during_cargo
          -- 오래된 판정으로 밸브를 열지 않는다. 그 배는 이미 떠났을 수 있다.
          AND assessed_at_utc > now() - interval '6 hours'
        ORDER BY call_sign, assessed_at_utc DESC
    )
    SELECT wharf_name,
           count(*) AS vessel_count,
           bool_or(level = ANY(CAST(:blocking AS text[]))) AS blocked,
           (array_agg(call_sign ORDER BY (level = ANY(CAST(:blocking AS text[]))) DESC))[1] AS lead_call_sign,
           (array_agg(vessel_name ORDER BY (level = ANY(CAST(:blocking AS text[]))) DESC))[1] AS lead_vessel_name,
           (array_agg(level ORDER BY (level = ANY(CAST(:blocking AS text[]))) DESC))[1] AS lead_level,
           (array_agg(reasons ORDER BY (level = ANY(CAST(:blocking AS text[]))) DESC))[1] AS lead_reasons,
           max(assessed_at_utc) AS assessed_at_utc
    FROM latest
    WHERE wharf_name IS NOT NULL
    GROUP BY wharf_name
    ORDER BY wharf_name
""")


def _gate_id(wharf_name: str) -> str:
    """계선시설명 → 게이트 식별자. 장치 토픽(`smartport/gate/{id}/…`)에 쓰인다."""
    return f"G-{wharf_name}"


async def build_gate_snapshot(db: AsyncSession, *, manual_overrides: dict | None = None) -> dict:
    """지금 게이트 상태 한 벌. 프런트 `useHardwareData` 가 기대하는 형태 그대로."""
    now = datetime.now(timezone.utc)
    overrides = manual_overrides or {}

    has_history = bool((await db.execute(_QUERY_TABLE_EXISTS)).scalar())
    if not has_history:
        # 판정 체계 미연결 — 모르면 닫는다.
        return {
            "gates": [],
            "assessment_connected": False,
            "note": "판정 이력(assessment_history)이 없어 모든 게이트를 인터락으로 둡니다.",
            "ts_utc": now.isoformat(),
        }

    rows = (
        await db.execute(
            _QUERY_GATE_SOURCE,
            {"during_cargo": AssessmentStage.DURING_CARGO.value, "blocking": list(_BLOCKING_VALUES)},
        )
    ).mappings().all()

    gates = []
    for row in rows:
        wharf = row["wharf_name"]
        gate_id = _gate_id(wharf)
        blocked = bool(row["blocked"])
        reasons = list(row["lead_reasons"] or [])

        if blocked:
            state, beacon, mode = STATE_INTERLOCK, BEACON_RED, MODE_AUTO
        else:
            # 판정이 통과해도 **자동으로 열지 않는다.** 하역 개시는 터미널의
            # 결정이다(회의 §2) — 우리는 "막을 이유가 없다"까지만 말한다.
            override = overrides.get(gate_id)
            if override == "APPROVE":
                state, beacon, mode = STATE_OPEN, BEACON_GREEN, MODE_MANUAL
            elif override == "BLOCK":
                state, beacon, mode = STATE_CLOSED, BEACON_AMBER, MODE_MANUAL
            else:
                state, beacon, mode = STATE_CLOSED, BEACON_AMBER, MODE_AUTO

        gates.append({
            "gate_id": gate_id,
            "name": f"{wharf} 하역 개시 인터락",
            "state": state,
            "mode": mode,
            "beacon": beacon,
            # 장치가 없으므로 페일세이프 회로 상태를 실제로는 모른다. 장치 연동
            # 전까지 True 로 두되, assessment_connected 와 함께 화면이 "장치
            # 미연결"임을 알 수 있게 한다 — 모르는 값을 아는 척하지 않는다.
            "fail_safe_ok": True,
            "ts_utc": now.isoformat(),
            # ↓ 판정 근거. 게이트가 왜 닫혔는지 관제사가 화면에서 바로 읽게 한다.
            "wharf_name": wharf,
            "level": row["lead_level"],
            "call_sign": row["lead_call_sign"],
            "vessel_name": row["lead_vessel_name"],
            "vessel_count": row["vessel_count"],
            "reasons": reasons,
            "assessed_at_utc": row["assessed_at_utc"].isoformat() if row["assessed_at_utc"] else None,
        })

    return {
        "gates": gates,
        "assessment_connected": True,
        "device_connected": False,  # MQTT 미연결 — 위 docstring 참고
        "ts_utc": now.isoformat(),
    }


def is_interlocked(snapshot: dict, gate_id: str) -> bool:
    """이 게이트가 인터락 상태인가. 모르는 게이트는 **인터락으로 본다**(모르면 닫는다)."""
    for gate in snapshot.get("gates", []):
        if gate["gate_id"] == gate_id:
            return gate["state"] == STATE_INTERLOCK
    return True
