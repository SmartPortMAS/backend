"""하드웨어 노드 릴레이 — 하역 개시 인터락 (D5).

경로 이름은 회의록의 `/ws/gate` 가 아니라 **`/ws/hardware`** 다. 프런트가
이미 그 이름을 기다리고 있기 때문이다:

    frontend/src/hooks/useHardwareData.js:4
      // 백엔드 /ws/hardware 릴레이가 완성되면 false 로 바꾸고 WebSocket 구독으로 교체

계약도 프런트가 먼저 확정해 뒀다(`mocks/mockHardware.js` 머리말 "★ MQTT 토픽 계약 ★").
새로 짓지 않고 그대로 지킨다 — 하드웨어 담당과 합의를 다시 거치지 않아도 된다.

    WS   /ws/hardware
         → { gates: [ {gate_id, name, state, mode, beacon, fail_safe_ok, ts_utc, …} ] }
           state  : OPEN | CLOSED | INTERLOCK
           beacon : GREEN | AMBER | RED
    POST /hardware/gates/{gate_id}/cmd   { command: APPROVE|BLOCK, requested_by }

[페일세이프를 백엔드에서도 강제한다]
  프런트는 이미 "INTERLOCK 은 원격 APPROVE 로 풀리지 않는다"를 구현해 뒀다
  (useHardwareData.js:24). 화면에서만 막으면 API 를 직접 호출해 뚫린다.
  같은 규칙을 여기서 다시 강제한다 — 안전 규칙은 **막을 수 있는 모든 계층에서**
  막는다. 뚫린 경로가 하나라도 있으면 그 규칙은 없는 것과 같다.

[수동 조작의 수명]
  APPROVE/BLOCK 은 프로세스 메모리에만 남는다(`_MANUAL_OVERRIDES`). 재시작하면
  사라지고 AUTO(=CLOSED)로 돌아간다. 이건 버그가 아니라 의도다 — 열림 상태가
  재시작을 넘어 살아남으면, 아무도 다시 판단하지 않은 채 밸브가 열려 있게 된다.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.database import AsyncSessionFactory
from app.services.gate_state import build_gate_snapshot, is_interlocked

logger = logging.getLogger("hardware")

router = APIRouter(tags=["hardware"])

PUSH_INTERVAL_SECONDS = 5.0

# gate_id -> 'APPROVE' | 'BLOCK'. 관제사의 수동 조작. 프로세스 메모리 전용(위 주석).
_MANUAL_OVERRIDES: dict[str, str] = {}


@router.websocket("/ws/hardware")
async def hardware_socket(websocket: WebSocket) -> None:
    """게이트 상태를 주기적으로 밀어 준다.

    판정(`assessment_history`)이 바뀌는 주기는 `watch_arrivals` 의 10분이지만,
    관제사의 수동 조작은 즉시 반영돼야 하므로 5초로 민다. 스냅샷이 작아
    (온산 스코프 계선시설 수준) 비용이 문제되지 않는다.
    """
    await websocket.accept()
    try:
        while True:
            async with AsyncSessionFactory() as db:
                snapshot = await build_gate_snapshot(db, manual_overrides=_MANUAL_OVERRIDES)
            await websocket.send_json(snapshot)
            await asyncio.sleep(PUSH_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        logger.info("hardware_socket: 클라이언트 연결 종료")
    except Exception:
        logger.exception("hardware_socket: 전송 실패 - 연결을 닫는다")
        await websocket.close()


@router.get("/hardware/gates", summary="게이트 상태 스냅샷 (WS 를 못 쓰는 클라이언트용)")
async def get_gates(db: AsyncSession = Depends(get_session)) -> dict:
    return await build_gate_snapshot(db, manual_overrides=_MANUAL_OVERRIDES)


class GateCommand(BaseModel):
    command: str = Field(description="APPROVE(하역 개시 허용) | BLOCK(차단)")
    requested_by: str = Field(description="조작한 관제사 식별자(자유 텍스트 — 인증 체계 없음)")


@router.post("/hardware/gates/{gate_id}/cmd", summary="게이트 수동 조작")
async def send_gate_command(
    gate_id: str, body: GateCommand, db: AsyncSession = Depends(get_session),
) -> dict:
    """관제사의 수동 조작.

    **INTERLOCK 상태는 APPROVE 로 풀리지 않는다** — 하드웨어 페일세이프 규칙이다.
    인터락을 풀려면 인터락을 만든 원인(하역중 판정이 부적합·판정불가)을 해소해야
    한다. 원격 버튼으로 안전 판정을 덮어쓸 수 있으면 그 판정은 없는 것과 같다.
    """
    command = body.command.upper().strip()
    if command not in ("APPROVE", "BLOCK"):
        raise HTTPException(status_code=422, detail="command 는 APPROVE 또는 BLOCK 이어야 합니다.")

    snapshot = await build_gate_snapshot(db, manual_overrides=_MANUAL_OVERRIDES)

    if command == "APPROVE" and is_interlocked(snapshot, gate_id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"게이트 '{gate_id}' 는 인터락 상태입니다 — 원격 승인으로 해제할 수 없습니다. "
                "하역중 판정이 '부적합' 또는 '판정불가'인 원인을 먼저 해소해야 합니다."
            ),
        )

    _MANUAL_OVERRIDES[gate_id] = command
    logger.info("gate_cmd: %s <- %s (by %s)", gate_id, command, body.requested_by)

    return await build_gate_snapshot(db, manual_overrides=_MANUAL_OVERRIDES)
