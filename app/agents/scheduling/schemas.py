"""스케줄링 에이전트 요청/응답 스키마.

CargoRef/AdjacentCargo는 안전관제 에이전트(app.agents.safety.schemas)와 동일한
모양을 그대로 재사용한다. 이 에이전트가 만드는 BerthCandidate.adjacent_cargos는
안전관제 에이전트의 SafetyAssessmentRequest.adjacent_cargos에 그대로 넣을 수
있는 형태로 설계했다 (safety/schemas.py 도입 당시부터 예고된 연결점).
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.agents.safety.schemas import AdjacentCargo, CargoRef


class OccupancyStatus(str, Enum):
    AVAILABLE = "여유"
    OCCUPIED = "점유"


class VesselSpec(BaseModel):
    draught_m: float = Field(gt=0, description="선박 흘수(m)")
    name_hint: str | None = Field(default=None, description="로그/에러 메시지용 참고 선명")


class SchedulingRequest(BaseModel):
    vessel: VesselSpec
    cargo: CargoRef
    window_start: datetime = Field(description="희망 접안 시작 시각(UTC)")
    window_end: datetime = Field(description="희망 접안 종료(출항 예정) 시각(UTC)")
    draught_margin_m: float = Field(default=1.0, ge=0, description="수심 대비 흘수 안전 여유(m)")

    @model_validator(mode="after")
    def _window_must_be_ordered(self) -> "SchedulingRequest":
        if self.window_end <= self.window_start:
            raise ValueError("window_end는 window_start보다 이후여야 합니다.")
        return self


class ConflictingPortCall(BaseModel):
    vessel_name: str | None = None
    arrival_at_utc: datetime | None = None
    departure_at_utc: datetime | None = None


class BerthCandidate(BaseModel):
    rank: int
    berth_id: str
    wharf_name: str
    port_name: str | None
    depth_m: float
    draught_margin_m: float = Field(description="depth_m - 요청 흘수(m). 클수록 여유")
    occupancy_status: OccupancyStatus
    conflicting_port_calls: list[ConflictingPortCall] = Field(default_factory=list)
    adjacent_cargos: list[AdjacentCargo] = Field(
        default_factory=list,
        description="인접 선석의 취급 화물. safety 에이전트 요청 바디로 그대로 전달 가능.",
    )


class SchedulingResult(BaseModel):
    target_cargo_name: str
    cargo_category: str
    candidates: list[BerthCandidate]
    total_eligible_count: int = Field(description="수심·화물 적합성만 통과한 선석 총 개수(점유 포함)")
