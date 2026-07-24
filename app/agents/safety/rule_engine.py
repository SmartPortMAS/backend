"""그래프 탐색 결과에 대한 결정적(rule-based) 위험등급 하한(floor) 계산.

LLM이 근거 없이 등급을 낮춰버리는 것을 막기 위한 안전장치다. LLM 최종 판단은
이 floor보다 높은 등급은 줄 수 있어도 낮출 수는 없다 (service.py에서 강제 보정).

기상 임계값(강풍/고파랑) 판정은 기획서상 기상분석 에이전트의 책임 범위라 이번
안전관제 에이전트의 rule engine에는 포함하지 않는다.
"""

from .schemas import RiskLevel, max_risk_level

# 이 두 카테고리가 동시에 걸리면(예: 산화제 + 가연성물질) 단순 혼재금지를 넘어
# 화재·폭발로 직결될 수 있는 조합으로 보고 배정불가로 판정한다.
SEVERE_CATEGORY_COMBO = {"산화제", "가연성물질"}


def compute_risk_floor(conflicts: list[dict]) -> RiskLevel:
    """MSDS 텍스트 마이닝 기반(INCOMPATIBLE_WITH) 충돌의 하한 등급."""
    if not conflicts:
        return RiskLevel.SAFE

    categories = {c["category"] for c in conflicts}
    if len(conflicts) >= 2 or SEVERE_CATEGORY_COMBO.issubset(categories):
        return RiskLevel.BLOCKED

    return RiskLevel.DANGER


# IMDG Code Chapter 7.2 격리 코드 -> RiskLevel 매핑.
# 코드 1(수평 3m 이상 이격)~4(종방향 완전 격창 분리)는 물리적 이격 거리를
# 규정한 것이지 "위험도 등급"이 아니다. 이걸 4단계 RiskLevel로 옮기는 것은
# IMDG가 정한 공식 환산표가 아니라 이번 구현에서 내린 해석이다:
#   1(away from)  -> 주의   : 최소 이격만 요구, 같은 격창 내 배치는 가능
#   2(separated from) -> 위험 : 서로 다른 격창 필요 — 화물 특성상 실질적 위험
#   3·4(완전 격창/종방향 분리) -> 배정불가 : "인접 선석" 자체가 이 요구를 구조적으로 위반
IMDG_CODE_TO_RISK_LEVEL: dict[str, RiskLevel] = {
    "1": RiskLevel.CAUTION,
    "2": RiskLevel.DANGER,
    "3": RiskLevel.BLOCKED,
    "4": RiskLevel.BLOCKED,
}


def compute_imdg_floor(imdg_conflicts: list[dict]) -> RiskLevel:
    """IMDG Code 공인 일반 격리표 기반 충돌의 하한 등급.

    여러 인접 화물과 동시에 격리 규정이 걸리면 그중 가장 심각한 등급을 채택한다.
    """
    floor = RiskLevel.SAFE
    for conflict in imdg_conflicts:
        level = IMDG_CODE_TO_RISK_LEVEL.get(conflict["segregation_code"], RiskLevel.SAFE)
        floor = max_risk_level(floor, level)
    return floor
