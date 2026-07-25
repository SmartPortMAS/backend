"""오케스트레이터 요청/응답 스키마.

스케줄링·안전관제·기상분석 세 에이전트의 출력을 종합한다(계획서 19p
"교차 검증 및 종합 의사결정"). 각 서브 에이전트의 스키마를 그대로 재사용해
불필요한 변환 계층을 두지 않는다.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.agents.safety.schemas import CargoRef, SafetyAssessmentResult
from app.agents.scheduling.schemas import AnchorageAssignment, BerthCandidate, VesselSpec
from app.agents.weather.schemas import WeatherAssessmentResult


class OverallDecision(str, Enum):
    """계획서 19p 로직의 4가지 귀결 + 온산 MVP 이식으로 추가된 5번째 상태.

    APPROVED 외 네 상태는 모두 "하역을 진행하면 안 되는" 상태라는 공통점이 있다 —
    관제사가 화면에서 한눈에 구분할 수 있도록 원인별로 분리했다.
    """

    APPROVED = "승인가능"
    WEATHER_BLOCKED = "기상불가_중단권고"
    NO_ELIGIBLE_BERTH = "적합선석없음"
    ALL_CANDIDATES_UNSAFE = "전후보배정불가"
    # 전용 선석 점유 + 대체 선석 없음(단독선석 등) -> 톤수에 맞는 정박지에서 대기
    # (온산 MVP 이식: scheduling.service.resolve_berth_assignment의 '정박지대기' 경로).
    WAITING_ANCHORAGE = "정박지대기"


class OrchestratorRequest(BaseModel):
    vessel: VesselSpec
    cargo: CargoRef
    window_start: datetime = Field(description="희망 접안 시작 시각(UTC)")
    window_end: datetime = Field(
        description="희망 접안 종료(출항 예정) 시각(UTC). 기상분석 에이전트의 "
        "예상 하역완료시각(expected_completion_at)으로도 그대로 쓰인다."
    )
    draught_margin_m: float = Field(default=1.0, ge=0, description="수심 대비 흘수 안전 여유(m)")
    weather_as_of: datetime | None = Field(
        default=None, description="기상 판단 기준 시각(UTC). 생략 시 서버 현재 시각(=지금 기상으로 판단)"
    )

    @model_validator(mode="after")
    def _window_must_be_ordered(self) -> "OrchestratorRequest":
        if self.window_end <= self.window_start:
            raise ValueError("window_end는 window_start보다 이후여야 합니다.")
        return self


class RejectedCandidate(BaseModel):
    """차순위 재탐색 과정에서 배정불가로 탈락한 후보의 기록 (추론 로그용)."""

    berth_id: str
    rank: int
    reason: str


class OrchestratorResult(BaseModel):
    overall_decision: OverallDecision
    selected_berth: BerthCandidate | None = Field(
        default=None, description="최종 추천 선석. 승인가능이 아니면 None"
    )
    safety_assessment: SafetyAssessmentResult | None = Field(
        default=None, description="선택된 선석에 대한 안전관제 결과. 기상불가/적합선석없음이면 None"
    )
    weather_assessment: WeatherAssessmentResult
    rejected_candidates: list[RejectedCandidate] = Field(
        default_factory=list, description="배정불가로 탈락해 재탐색된 후보 이력"
    )
    anchorage_assignment: AnchorageAssignment | None = Field(
        default=None, description="overall_decision이 정박지대기일 때만 채워짐(온산 MVP 이식)"
    )
    assignment_trace: list[str] = Field(
        default_factory=list,
        description="전용/대체/정박지대기 판단 경로와 근거(온산 MVP 이식: 팀원 오케스트레이터의 "
        "berth_decision.trace와 동일한 목적)",
    )
    summary: str = Field(description="관제사가 읽을 종합 의견 (1~2문단)")


class LLMSummary(BaseModel):
    """Gemini response_schema로 강제할 최종 종합 의견 구조화 출력."""

    summary: str = Field(description="관제사가 바로 읽을 수 있는 종합 의견, 1~2문단 한국어")
