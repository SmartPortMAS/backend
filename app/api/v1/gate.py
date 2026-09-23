"""하역 개시 인터락 게이트 — 화면이 쓰는 창구. 실제 일은 app/gate/bridge.py 가 한다.

  GET  /api/v1/gate/state           지금 상태 한 장 (WebSocket 못 쓰는 곳용)
  POST /api/v1/gate/{id}/cmd        {"action":"OPEN"|"CLOSE"} → 장치에 그대로 전달
  POST /api/v1/gate/demo/weather    {"wind_ms": 16, "wave_m": 0.5} 시연 입력 (둘 다 없으면 해제)
  DELETE /api/v1/gate/demo/weather  시연 입력 해제
  WS   /ws/gate                     1초마다 상태 한 장 푸시
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.gate.bridge import bridge

router = APIRouter(prefix="/gate", tags=["gate"])
ws_router = APIRouter()


class GateCommand(BaseModel):
    action: Literal["OPEN", "CLOSE"]


class DemoWeather(BaseModel):
    wind_ms: float | None = Field(None, ge=0, le=60, description="시연 풍속 (m/s). 없으면 실측")
    wave_m: float | None = Field(None, ge=0, le=15, description="시연 파고 (m). 없으면 실측")


@router.get("/state", summary="게이트 상태 한 장")
async def get_state() -> dict:
    return bridge.snapshot()


@router.post("/{gate_id}/cmd", summary="하역 개시 요청 / 하역 중단 — 장치에 전달")
async def post_cmd(gate_id: str, body: GateCommand) -> dict:
    if gate_id not in bridge.berth_of:
        raise HTTPException(status_code=404, detail=f"모르는 게이트: {gate_id}")
    ok = bridge.publish_cmd(gate_id, body.action)
    if not ok:
        raise HTTPException(status_code=503, detail="브로커에 보내지 못했습니다 — mosquitto 가 떠 있는지 확인")
    # 거부 여부는 여기서 정하지 않는다. 장치가 다음 status 의 denied 로 답한다.
    return {"gate_id": gate_id, "action": body.action, "published": True,
            "note": "결과는 다음 status 의 denied·last_result 로 온다"}


@router.post("/demo/weather", summary="시연 입력 — 풍속·파고 주입 (판정 규칙은 실제와 같음)")
async def set_demo_weather(body: DemoWeather) -> dict:
    bridge.set_demo(body.wind_ms, body.wave_m)
    await bridge.evaluate_once()
    return bridge.snapshot()


@router.delete("/demo/weather", summary="시연 입력 해제 — 실측으로 복귀")
async def clear_demo_weather() -> dict:
    bridge.set_demo(None, None)
    await bridge.evaluate_once()
    return bridge.snapshot()


@ws_router.websocket("/ws/gate")
async def ws_gate(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            await ws.send_json(bridge.snapshot())
            try:
                # 1초 기다리며 클라이언트가 보낸 것(핑·닫힘)을 받는다 — 닫힘을 여기서 알아챈다
                await asyncio.wait_for(ws.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
