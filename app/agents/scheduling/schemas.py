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
    dwt_t: float | None = Field(
        default=None,
        description="재화중량톤수(DWT). SUBSTITUTABLE_WITH 대체 게이트(to_max_dwt)와 "
        "정박지 톤수 배정에 쓰인다. 생략하면 흘수만으로 판단(대체 게이트의 DWT 조건은 "
        "건너뜀 — 온산 MVP 이식, 9장 참고).",
    )
    name_hint: str | None = Field(default=None, description="로그/에러 메시지용 참고 선명")
    call_sign: str | None = Field(
        default=None,
        description="이 배의 호출부호. 점유 판정에서 '자기 자신이 잡아 둔 예약'을 "
        "제외하는 데 쓴다 — 이 값이 없으면 추천을 받은 배가 재판정 때 자기 예약에 "
        "막혀 영원히 승인할 수 없다(2026-08-21 실측).",
    )


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
    berth_group: str | None = Field(
        default=None,
        description="berth_weather_threshold.berth_group 매핑값. 있으면 오케스트레이터가 "
        "이 선석 전용 기상 임계값으로 재판정할 수 있다(온산 MVP 이식 — '같은 기상, "
        "선석마다 다른 판정' 차별점). None은 '온산 스코프 밖'이 아니라 '이 선석의 "
        "기상 임계값 자료가 없다'는 뜻이다 — 온산 소속 여부는 onsan_scope로 판단할 것. "
        "실제로 온산 S-Oil 부이 2기는 onsan_scope=True이면서 berth_group=None이다.",
    )
    onsan_scope: bool = Field(
        default=False,
        description="온산 MVP 대상 선석인지(berth_neo4j_loader.ONSAN_SCOPE_WHARF_NAMES 15개, "
        "달포부두 포함 2026-08-21). [2026-08-21] find_eligible_berths가 이 값을 "
        "WHERE 하드 필터로 쓰므로 여기 도달하는 후보는 전부 true다 — 정렬 기준으로는 "
        "여전히 변별력이 없어(08_스케줄링_전면재설계_자동배정_설계문서.md §4.1.3-A) "
        "쓰지 않고, 값 자체는 참고용으로 계속 내려준다.",
    )
    draught_margin_m: float = Field(description="depth_m - 요청 흘수(m). 클수록 여유")
    occupancy_status: OccupancyStatus
    conflicting_port_calls: list[ConflictingPortCall] = Field(default_factory=list)
    adjacent_cargos: list[AdjacentCargo] = Field(
        default_factory=list,
        description="인접 선석의 취급 화물. safety 에이전트 요청 바디로 그대로 전달 가능.",
    )
    latitude: float | None = Field(
        default=None, description="선석 대표 좌표 위도 — 점유 시 최근접 대체 탐색용(§5.2.1-C)"
    )
    longitude: float | None = Field(
        default=None, description="선석 대표 좌표 경도 — 점유 시 최근접 대체 탐색용(§5.2.1-C)"
    )
    unload_capacity: float | None = Field(
        default=None,
        description="선석 하역능력(upa_berth_facility.unload_capacity). 단위·산출기준 미확인이라 "
        "소프트 타이브레이커로만 쓴다(§5.2.1-B) — 결측이면 순위에서만 불리하고 후보에서 제외되지 않는다.",
    )


class SchedulingResult(BaseModel):
    target_cargo_name: str
    cargo_category: str
    candidates: list[BerthCandidate]
    total_eligible_count: int = Field(description="수심·화물 적합성만 통과한 선석 총 개수(점유 포함)")


class AnchorageAssignment(BaseModel):
    """정박지 대기 배정 (온산 MVP 이식: build_anchorage_assignment.py의 assign_anchorage 모델).

    전용 선석이 점유 중이고 대체 가능한 선석도 없을 때(단독선석 등) 도달하는 최종 상태.
    """

    anchorage_id: str
    name: str
    tonnage_rule: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class BerthResolution(BaseModel):
    """전용 선석이 점유 중일 때의 '전용 -> 대체 -> 정박지 대기' 3단계 배정 결과.

    scheduling.service.resolve_berth_assignment가 만든다. 오케스트레이터가 후보별로
    이걸 호출해 실제 배정 가능 여부와 그 근거(trace)를 얻는다.
    """

    path: str = Field(description="전용 | 대체 | 정박지대기 | 배정불가")
    berth: BerthCandidate | None = Field(default=None, description="path가 전용/대체일 때만 채워짐")
    anchorage: AnchorageAssignment | None = Field(default=None, description="path가 정박지대기일 때만 채워짐")
    trace: list[str] = Field(default_factory=list, description="판단 경로와 근거(관제사용 설명)")
