"""안전관제 에이전트 요청/응답 스키마.

스케줄링 에이전트와 선석(Berth) 지식그래프가 아직 없으므로, 인접 선석의
취급 화물 정보는 이 API의 요청 바디로 직접 받는다. 추후 스케줄링 에이전트가
생기면 그 출력(추천 선석 + 인접 선석 화물 목록)을 그대로 이 요청 바디로
매핑해 연결하면 된다.
"""

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class RiskLevel(str, Enum):
    """4단계 위험등급. 값 순서가 곧 심각도 순서(SAFE < CAUTION < DANGER < BLOCKED)."""

    SAFE = "안전"
    CAUTION = "주의"
    DANGER = "위험"
    BLOCKED = "배정불가"


_RISK_LEVEL_ORDER: dict[RiskLevel, int] = {
    RiskLevel.SAFE: 0,
    RiskLevel.CAUTION: 1,
    RiskLevel.DANGER: 2,
    RiskLevel.BLOCKED: 3,
}


def risk_level_rank(level: RiskLevel) -> int:
    return _RISK_LEVEL_ORDER[level]


def max_risk_level(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    return a if risk_level_rank(a) >= risk_level_rank(b) else b


class CargoRef(BaseModel):
    """화물 식별자. chem_id(Neo4j Chemical.id / msds_chemical.chem_id) 또는
    cas_no 중 최소 하나는 있어야 한다."""

    chem_id: str | None = None
    cas_no: str | None = None
    name_hint: str | None = Field(default=None, description="식별 실패 시 로그/에러 메시지용 참고 이름")

    @model_validator(mode="after")
    def _require_one_identifier(self) -> "CargoRef":
        if not self.chem_id and not self.cas_no:
            raise ValueError("CargoRef는 chem_id 또는 cas_no 중 하나가 반드시 있어야 합니다.")
        return self


class AdjacentCargo(BaseModel):
    berth_name: str
    cargo: CargoRef


class SafetyAssessmentRequest(BaseModel):
    """기상 데이터는 의도적으로 포함하지 않는다. 기상 임계값 기반 작업가능 여부 판정은
    기상분석 에이전트의 책임 범위이고, 안전관제 에이전트는 화물/MSDS/혼재금지 판단만 담당한다.
    기상×안전 조합 판단은 각 에이전트 결과를 받는 상위 오케스트레이터에서 처리한다."""

    target_cargo: CargoRef
    adjacent_cargos: list[AdjacentCargo] = Field(default_factory=list)


class IncompatibleConflict(BaseModel):
    adjacent_berth: str
    adjacent_chem_id: str
    adjacent_name: str
    shared_category: str = Field(description="혼재금지 매칭이 발생한 IncompatibleMaterial 카테고리명")
    direction: str = Field(description="target_incompatible_with_adjacent | adjacent_incompatible_with_target")


class LLMAssessment(BaseModel):
    """Gemini response_schema로 강제할 구조화 출력. LLM은 이 필드만 채운다."""

    risk_level: RiskLevel
    checklist: list[str] = Field(description="화물 맞춤형 안전 체크리스트 (MSDS 문구 근거)")
    key_hazards: list[str] = Field(description="핵심 유해성 요약 (2~5개)")
    reasoning: str = Field(description="판단 근거 요약 (관제사가 읽을 한두 문단)")


class SafetyAssessmentResult(BaseModel):
    target_cargo_name: str
    risk_level: RiskLevel
    checklist: list[str]
    key_hazards: list[str]
    reasoning: str
    conflicts: list[IncompatibleConflict]
    rule_engine_floor: RiskLevel = Field(description="그래프 탐색 기반 결정적 하한 등급")
    msds_sections_used: list[str] = Field(description="프롬프트 근거로 사용된 MSDS detail 섹션 키 목록")
