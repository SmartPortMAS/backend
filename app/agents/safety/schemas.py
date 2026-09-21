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
    # [2026-09-22] "배정불가" -> "하역불가". 우리는 배정을 거부하는 주체가 아니다.
    # 이 등급이 실제로 말하는 것은 "이 화물 조합을 이 자리에서 하역하면 안 된다"이고,
    # 그 판단은 우리가 할 수 있다. 자리를 주고 말고는 선석회의·VTS·터미널의 일이다.
    #
    # 판정 등급(AssessmentLevel: 적합/주의/부적합/판정불가)과는 **여전히 별개다**.
    # 이쪽은 안전 에이전트의 위험 척도이고, 저쪽은 시스템의 산출물이다
    # (app/models/assessment_history.py 의 '등급을 RiskLevel 과 분리한 이유' 참고).
    BLOCKED = "하역불가"


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
    unload_method_name: str | None = Field(
        default=None,
        description="이번 하역에 신고된 하역 방식(예: '펌프', '크레인'). msds_chemical이 아니라 "
        "화물 manifest(선적건)마다 다른 값이라 chem_id로는 알 수 없어 호출부가 직접 넘긴다. "
        "target_cargo에만 의미가 있다(포장기준 판정은 대상 화물 자신에 대한 것 — 인접 화물 "
        "혼재금지와는 다른 축의 판정). 생략하면 포장기준 판정 자체를 하지 않는다.",
    )

    @model_validator(mode="after")
    def _require_one_identifier(self) -> "CargoRef":
        if not self.chem_id and not self.cas_no:
            raise ValueError("CargoRef는 chem_id 또는 cas_no 중 하나가 반드시 있어야 합니다.")
        return self


class AdjacentCargo(BaseModel):
    berth_name: str
    cargo: CargoRef
    distance_m: float | None = Field(
        default=None,
        description="대상 선석과의 실측 거리(m, berth_neo4j_loader의 좌표 계산). "
        "PILOT_ADJACENT_PAIRS(수동 큐레이션) 유래거나 좌표 결측이면 None — "
        "[2026-08-23] 판정에는 쓰지 않는다 — 부두 간 거리는 IMDG 규율 대상이 "
        "아니어서 표시용으로만 내려준다(rule_engine.compute_imdg_berth_adjacency_floor 참고).",
    )


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


class ImdgSegregationConflict(BaseModel):
    """IMDG Code Chapter 7.2 공인 일반 격리표 기반 충돌.

    IncompatibleConflict(MSDS 텍스트 마이닝)와는 별도 신호 — 화물 개별 반응성이
    아니라 위험물 대분류(Class) 간 국제 공인 규정에 근거한다.
    """

    adjacent_berth: str
    adjacent_chem_id: str
    adjacent_name: str
    target_imdg_class: str
    adjacent_imdg_class: str
    segregation_code: str = Field(description="IMDG 격리 코드(1~4). 클수록 강한 물리적 이격 요구")
    distance_m: float | None = Field(
        default=None, description="실측 거리(m). None이면 거리 미상 — 등급 판정 시 보수적으로 처리됨"
    )


class ImdgUnconfirmedPair(BaseModel):
    """두 화물의 IMDG Class가 모두 확인됐으나 SEGREGATE 관계(공인 격리규정)가
    그래프에 없는 경우. ImdgSegregationConflict(코드 1~4 확인됨)와 달리 "격리
    불필요"인지 "개별 확인 필요(코드 X)"인지 로더가 구분해 저장하지 않으므로,
    자동 판정을 내리지 않고 관제사 확인 대상으로만 노출한다."""

    adjacent_berth: str
    adjacent_chem_id: str
    adjacent_name: str
    target_imdg_class: str
    adjacent_imdg_class: str


class UnassessedPair(BaseModel):
    """혼재금지 판정 근거가 불완전했던 인접 화물 (2026-08-23 추가).

    "충돌 없음"에는 성격이 다른 두 가지가 섞여 있다 — 양쪽 근거를 다 보고
    겹치는 게 없었던 경우와, 애초에 볼 근거가 없어 아무것도 못 걸러낸 경우다.
    후자를 안전으로 표시하면 관제사가 "확인됐다"로 오독한다. 이 목록이 그
    구분을 드러낸다.

    실측 근거(2026-08-23): 36종 1,260개 순서쌍 중 판정 근거가 불완전한 쌍이
    200건(22.1%)이었고, 원인은 KOSHA MSDS의 J08("피해야 할 물질")이 27/36종에서
    "자료없음"이라는 데 있다(KOSHA API 직접 호출로 원천 확인).
    """

    adjacent_berth: str
    adjacent_chem_id: str
    adjacent_name: str
    assessability: str = Field(
        description="단방향판정(한쪽 근거만 있음) 또는 판정불가(양방향 모두 근거 없음)"
    )
    reason: str = Field(description="어느 근거가 없어서 판정하지 못했는지")


class BulkCompatibilityConflict(BaseModel):
    """벌크 액체화학물질 호환성 그룹(참고자료 기반, bulk_compatibility.py) 충돌.

    MSDS 텍스트 마이닝(IncompatibleConflict), IMDG 공인 격리표
    (ImdgSegregationConflict)와 근거가 다른 세 번째 참고 신호 — 이 축이 나타내는
    출처·적용범위의 한계는 bulk_compatibility.py 모듈 docstring 참고."""

    adjacent_berth: str
    adjacent_chem_id: str
    adjacent_name: str
    target_group: int
    target_group_name: str
    adjacent_group: int
    adjacent_group_name: str
    reason: str


class PackagingViolation(BaseModel):
    """포장·하역방식 부적합. IncompatibleConflict/ImdgSegregationConflict와 달리
    인접 화물이 아니라 대상 화물 자신의 신고 내용(용기등급 vs 하역방식)만으로 판정한다."""

    packing_group: str
    unload_method_name: str
    reason: str


class LLMAssessment(BaseModel):
    """LLM 구조화 출력. **등급(risk_level)은 여기 없다.**

    [2026-08-23] risk_level을 뺐다. 이전에는 LLM이 등급을 정하고 코드가
    rule_engine_floor로 하한만 보정했는데, 실측 결과 LLM의 등급이 입력 근거와
    무관하게 움직였다:
      · 충돌 0건인 조합에서도 24/24회 '위험'으로 격상 (근거 없이)
      · 충돌 1건인 조합도 '위험' — 즉 0건과 1건을 구분하지 못했다
      · 48회 측정에서 '안전'이 단 한 번도 나오지 않아, 4단계 등급이 사실상
        '위험' 하나로 수렴했다
    원인도 규명됐다 — 프롬프트의 86%가 대상 화물 자체의 유해성 문구(벤젠 기준
    3,814자)인데 risk_level이 '무엇에 대한 등급'인지 정의가 없어서, 모델이
    "이 배치가 위험한가"가 아니라 "이 화물이 위험한 물질인가"에 답하고 있었다
    (ablation: 발췌 제거 또는 정의 추가 시 각각 100% 교정됨).

    등급은 이제 규칙엔진이 확정한다(service.assess_safety). LLM은 원래 잘하는
    것 — MSDS 근거에 기반한 서술 — 만 맡는다. 부수 효과로 등급이 결정적이 되어
    같은 입력에 항상 같은 등급이 나오고, LLM을 기다리지 않고도 등급을 확정할 수
    있다(45ms, /safety/verdict).
    """

    checklist: list[str] = Field(description="화물 맞춤형 안전 체크리스트 (MSDS 문구 근거)")
    key_hazards: list[str] = Field(description="핵심 유해성 요약 (2~5개)")
    reasoning: str = Field(description="판단 근거 요약 (관제사가 읽을 한두 문단)")


class SafetyVerdict(BaseModel):
    """LLM 없이 확정되는 판정 — `POST /safety/verdict` 응답 (2026-08-23 추가).

    SafetyAssessmentResult에서 LLM 생성 필드(checklist·key_hazards·reasoning)만
    뺀 모양이다. 화면이 결론을 먼저 띄우고 서술을 이어서 채우도록 하기 위한
    것으로, 여기 실린 risk_level은 나중에 /safety/assess가 주는 값과 항상
    같다 — 등급을 규칙엔진이 확정하므로 뒤집히지 않는다.
    """

    target_cargo_name: str
    risk_level: RiskLevel = Field(
        description="규칙엔진이 확정한 등급. /safety/assess의 risk_level과 동일한 값이다."
    )
    rule_engine_floor: RiskLevel
    conflicts: list[IncompatibleConflict] = Field(default_factory=list)
    imdg_conflicts: list[ImdgSegregationConflict] = Field(default_factory=list)
    imdg_unconfirmed_pairs: list[ImdgUnconfirmedPair] = Field(default_factory=list)
    bulk_compatibility_conflicts: list[BulkCompatibilityConflict] = Field(default_factory=list)
    packaging_violations: list[PackagingViolation] = Field(default_factory=list)
    unassessed_pairs: list[UnassessedPair] = Field(default_factory=list)
    msds_sections_used: list[str] = Field(default_factory=list)
    imdg_classes: dict[str, str] = Field(default_factory=dict)


class SafetyAssessmentResult(BaseModel):
    target_cargo_name: str
    risk_level: RiskLevel
    checklist: list[str]
    key_hazards: list[str]
    reasoning: str
    conflicts: list[IncompatibleConflict]
    imdg_conflicts: list[ImdgSegregationConflict] = Field(
        default_factory=list, description="IMDG Code 공인 일반 격리표 기반 충돌"
    )
    imdg_unconfirmed_pairs: list[ImdgUnconfirmedPair] = Field(
        default_factory=list,
        description="두 화물의 IMDG Class는 모두 알려져 있으나 SEGREGATE 관계가 없어 "
        "코드 X(개별 확인 필요)인지 로더 미등재인지 구분되지 않는 화물쌍. "
        "rule_engine_floor를 최소 '주의'로 격상시키는 근거이며, 자동으로 배정불가 "
        "처리하지는 않는다 — 도메인 전문가 확인 전까지의 잠정 조치.",
    )
    bulk_compatibility_conflicts: list[BulkCompatibilityConflict] = Field(
        default_factory=list,
        description="벌크 액체화학물질 호환성 그룹(참고자료 기반) 충돌 — 국내/국제 법적 "
        "구속력이 확정된 축이 아니라 제3의 참고 신호. bulk_compatibility.py 참고.",
    )
    packaging_violations: list[PackagingViolation] = Field(
        default_factory=list,
        description="용기등급 대비 하역방식 부적합 (target_cargo.unload_method_name을 넘긴 경우만 판정됨)",
    )
    unassessed_pairs: list["UnassessedPair"] = Field(
        default_factory=list,
        description="혼재금지 판정 근거가 불완전한 인접 화물. 여기 실린 화물은 "
        "'충돌 없음'이 '안전 확인'을 뜻하지 않는다 — 볼 근거가 없어 아무것도 "
        "걸러내지 못한 것이다. rule_engine_floor를 최소 '주의'로 격상시킨다. "
        "원인은 대개 KOSHA MSDS의 J08('피해야 할 물질')이 '자료없음'인 것으로, "
        "2026-08-23 API 원천 확인 결과 36종 중 27종이 해당한다.",
    )
    rule_engine_floor: RiskLevel = Field(description="그래프 탐색 기반 결정적 하한 등급 (MSDS 텍스트 + 벌크 호환성그룹 + 포장기준 + 판정가능성 중 가장 심각한 쪽)")
    msds_sections_used: list[str] = Field(description="프롬프트 근거로 사용된 MSDS detail 섹션 키 목록")
    imdg_classes: dict[str, str] = Field(
        default_factory=dict,
        description="대상·인접 화물의 chem_id -> IMDG Class 코드. imdg_conflicts에 안 걸린 "
        "화물쌍도 각자 Class 자체는 여기서 알 수 있다 — 화면이 'SEGREGATE 관계 없음'을 "
        "'공인 규정상 X(격리 불필요, 두 Class 모두 알려짐)'와 '이 Class 조합이 그래프에 "
        "안 실려서 모름'을 구분해 보여주는 데 쓴다. 값이 없는 chem_id는 HAS_IMDG_CLASS "
        "관계 자체가 없다는 뜻이다.",
    )
