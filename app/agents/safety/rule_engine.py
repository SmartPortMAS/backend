"""그래프 탐색 결과에 대한 결정적(rule-based) 위험등급 하한(floor) 계산.

LLM이 근거 없이 등급을 낮춰버리는 것을 막기 위한 안전장치다. LLM 최종 판단은
이 floor보다 높은 등급은 줄 수 있어도 낮출 수는 없다 (service.py에서 강제 보정).

기상 임계값(강풍/고파랑) 판정은 기획서상 기상분석 에이전트의 책임 범위라 이번
안전관제 에이전트의 rule engine에는 포함하지 않는다.
"""

from .schemas import RiskLevel

# 이 두 카테고리가 동시에 걸리면(예: 산화제 + 가연성물질) 단순 혼재금지를 넘어
# 화재·폭발로 직결될 수 있는 조합으로 보고 배정불가로 판정한다.
SEVERE_CATEGORY_COMBO = {"산화제", "가연성물질"}


def compute_risk_floor(conflicts: list[dict]) -> RiskLevel:
    if not conflicts:
        return RiskLevel.SAFE

    categories = {c["category"] for c in conflicts}
    if len(conflicts) >= 2 or SEVERE_CATEGORY_COMBO.issubset(categories):
        return RiskLevel.BLOCKED

    return RiskLevel.DANGER
