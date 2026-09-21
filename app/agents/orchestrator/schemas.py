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

    [2026-09-22] 값에서 **우리가 승인·배정한다는 뜻**을 걷어냈다.

      "승인가능"      -> "적합"        우리는 승인하지 않는다. 조건에 맞는지만 말한다.
      "전후보배정불가" -> "전 후보 부적합"  우리는 배정을 거부하는 주체가 아니다.

    '배정'이라는 단어 자체를 지운 것이 아니다 — `NO_ELIGIBLE_BERTH`("적합선석없음")나
    판정 근거의 "배정된 선석이…"는 **남이 한 배정**을 가리키는 정확한 서술이라 그대로
    둔다. 문제는 그 배정을 우리가 한다고 읽히는 어휘였다.

    이 값은 DB 에 저장되지 않는다(`assessment_history` 는 `AssessmentLevel` 을 쓴다).
    화면이 `decision_label` 로 그대로 표시하므로 값이 곧 관제사가 읽는 문장이다.
    """

    APPROVED = "적합"
    WEATHER_BLOCKED = "기상불가_중단권고"
    NO_ELIGIBLE_BERTH = "적합선석없음"
    ALL_CANDIDATES_UNSAFE = "전 후보 부적합"
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
    assigned_wharf_name: str | None = Field(
        default=None,
        description="이미 정해진 선석(예: 실시간 위치 조인의 현재 접안 선석명). 있으면 "
        "top-3 재탐색 대신 이 선석 하나만 검증한다(검증모드). 없으면 기존 top-3 "
        "탐색 경로(탐색모드, 하위 호환).",
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
    assignment_changed: bool = Field(
        default=False,
        description="검증모드(assigned_wharf_name 지정)에서, 최종 selected_berth.wharf_name이 "
        "assigned_wharf_name과 다르면 True — 원래 있던 자리가 아니라 대체 선석으로 바뀌었다는 "
        "뜻이라 관제사가 바로 알아야 한다. 탐색모드에서는 항상 False.",
    )
    suggested_alternatives: list[BerthCandidate] = Field(
        default_factory=list,
        description="배정된 시설이 부적합할 때 내놓는 **대체 선석 제안**(최대 3). "
        "9/17 회의 §3 의 조치안 '대체선석'이다. **의견일 뿐 배정이 아니다** — 어떤 "
        "자리도 잠그지 않고, 실제로 옮길지는 선석회의·VTS·터미널이 정한다. "
        "적합 판정이면 비어 있다(옮길 이유가 없으므로).",
    )
    suggestion_note: str | None = Field(
        default=None,
        description="대체안을 못 찾았을 때 그 이유. 후보가 있으면 None. "
        "'없음'과 '못 찾음'을 구분하려고 둔다.",
    )
    evidence_missing: bool = Field(
        default=False,
        description="결론이 '근거 부족'에서 나왔는가. True 면 판정 자체를 못 한 것이고"
        "(계선시설 표기 미해소·조위 예보 없음 등), False 면 근거를 갖추고 내린 판정이다. "
        "회의 §4 '근거 부족을 안전과 구분' — 이 값이 assessment_history.level 에서 "
        "'판정불가'와 '부적합'을 가른다.",
    )
    summary: str = Field(description="관제사가 읽을 종합 의견 (1~2문단)")
    berth_match_summary: str | None = Field(
        default=None,
        description="선석배정현황 팝업 전용 — 선석 스펙과 선박 매칭만 다루는 LLM 한 문장 "
        "요약(화학물질·안전판정 내용 제외). summary와 같은 LLM 호출에서 함께 받는다"
        "(호출 두 번 비용 방지). selected_berth가 없으면 None.",
    )

    def decision_detail(self) -> dict:
        """관제사 화면(선석배정현황 팝업 등)에 보여줄 구조화된 배정 근거.

        `summary`는 LLM이 쓴 자유문이라 특정 요인(흔히 흘수)만 강조하고 실제로
        통과한 다른 조건(카테고리/전용-대체 경로/기상)은 생략할 수 있다 — 실제로
        관제사가 "수심 얘기만 있고 다른 근거는 안 보인다"고 지적한 사례가 있었다
        (2026-08-19). 이 메서드는 판정에 실제로 쓰인 구조화 값을 그대로 담아서,
        요약문이 무엇을 강조했든 전체 근거를 화면에서 확인할 수 있게 한다.

        안전판정(화학물질 위험성·혼재 등)은 일부러 안 담는다(2026-08-19 재수정) —
        그건 안전관제 에이전트의 몫이고 이미 별도 화면(선박 상세·안전심사)에서
        보여준다. 여기는 "선석 스펙과 선박이 어떻게 매칭됐는가"(수심·흘수·전용
        /대체 경로)만 다뤄야 두 화면의 책임이 안 섞인다.
        """
        detail: dict = {"trace": self.assignment_trace}
        if self.berth_match_summary:
            detail["narrative"] = self.berth_match_summary
        if self.selected_berth:
            detail["berth"] = {
                "rank": self.selected_berth.rank,
                "berth_group": self.selected_berth.berth_group,
                "depth_m": self.selected_berth.depth_m,
                "draught_margin_m": self.selected_berth.draught_margin_m,
                "occupancy_status": self.selected_berth.occupancy_status.value,
            }
        detail["weather"] = {
            "status": self.weather_assessment.status.value,
            "reasons": self.weather_assessment.reasons,
        }
        if self.rejected_candidates:
            detail["rejected_candidates"] = [
                {"berth_id": rc.berth_id, "rank": rc.rank, "reason": rc.reason}
                for rc in self.rejected_candidates
            ]
        return detail


class LLMSummary(BaseModel):
    """Gemini response_schema로 강제할 최종 종합 의견 구조화 출력."""

    summary: str = Field(description="관제사가 바로 읽을 수 있는 종합 의견, 1~2문단 한국어")
    berth_match_summary: str = Field(
        description="선석 스펙(수심·정원·전용/대체 여부)이 이 선박과 왜 맞는지만 다루는 "
        "한 문장. 화학물질명·위험등급·유해성 등 안전판정 내용은 절대 넣지 말 것 — "
        "선석 배정 근거 화면 전용이라 안전 얘기가 섞이면 안 된다.",
    )
