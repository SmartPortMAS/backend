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

# 2026-08-16 추가 — 코드 1·2는 "실제로 얼마나 떨어져 있는지"를 반영한다.
# 이 항의 실측 부두 간격(60개 부두 최근접거리 중앙값 316.5m)에 맞춰 잡은
# 운영 정책값이다 — IMDG 자체엔 부두 단위(선박 대 선박) 이격거리 규정이
# 없다(원 규정은 선내 화물창 포장 간격 기준, 3m 단위라 항 규모에 안 맞음).
# 소방청 위험물안전관리법의 육상 저장탱크 이격거리(3~30m)보다는 크게,
# 실측으로 못 찾은 대용량 위험물 저장시설 기준(수백m대로 추정)과는 같은
# 자릿수로 잡았다 — 규정으로 검증된 값이 아니므로 추후 항만 안전 전문
# 자료 확보 시 조정 대상이다.
#
# 코드 3·4는 거리로 대체할 수 없다("완전 격창 분리" 요구 자체가 표면
# 거리 개념이 아님) — 이격거리 무관하게 항상 배정불가 유지.
IMDG_CODE_DISTANCE_THRESHOLD_M: dict[str, float] = {
    "1": 300.0,
    "2": 400.0,
}


def compute_imdg_floor(imdg_conflicts: list[dict]) -> RiskLevel:
    """IMDG Code 공인 일반 격리표 기반 충돌의 하한 등급.

    여러 인접 화물과 동시에 격리 규정이 걸리면 그중 가장 심각한 등급을 채택한다.

    코드 1·2는 실측 거리(distance_m)가 임계값 이상으로 "확인"된 경우에만
    SAFE로 완화한다 — distance_m이 None(거리 모름, 예: 수동 큐레이션 인접쌍)이면
    기존처럼 코드 자체의 등급을 그대로 적용한다. "모르면 안전하다고 보지 않는다"
    원칙(V-DG-01과 같은 성격) — 결측을 위험 완화 근거로 쓰지 않는다.
    """
    floor = RiskLevel.SAFE
    for conflict in imdg_conflicts:
        code = conflict["segregation_code"]
        threshold = IMDG_CODE_DISTANCE_THRESHOLD_M.get(code)
        distance_m = conflict.get("distance_m")
        if threshold is not None and distance_m is not None and distance_m >= threshold:
            level = RiskLevel.SAFE
        else:
            level = IMDG_CODE_TO_RISK_LEVEL.get(code, RiskLevel.SAFE)
        floor = max_risk_level(floor, level)
    return floor


# ─────────────────────────────────────────────────────────────────────────────
# 포장·하역방식 부적합 — 인접 화물이 아니라 대상 화물 자신의 신고 내용만으로
# 판정한다(위 두 floor와 다른 축). 실측(upa_cargo_manifest): unload_method_name은
# '펌프'·'크레인' 둘뿐이고, packing_group은 Ⅰ/Ⅱ/Ⅲ 또는 결측이다.
#
# 근거: 「위험물 선박운송 및 저장규칙」제20조·「위험물 선박운송 기준」 —
# 용기등급 Ⅰ(고위험)은 전용 용기·펌프 이송이 필요하다는 취지. 정확한 승인
# 방식 목록은 법정 고시가 아니라 터미널·화물별 운영규정으로 갈리므로, 여기
# 값은 관행값이다(berth_draught_check의 UKC 10%와 같은 성격의 한계 — 실제
# 운영규정 확보 전까지는 최소 기준으로만 쓸 것).
# ─────────────────────────────────────────────────────────────────────────────
PACKING_GROUP_HIGH_RISK = "Ⅰ"
APPROVED_METHODS_FOR_HIGH_RISK_PACKING = frozenset({"펌프"})


def find_packing_violation(
    packing_group: str | None, unload_method_name: str | None
) -> dict | None:
    """용기등급 대비 하역방식 부적합을 찾는다. 위반이 없거나 판정 불가면 None.

    unload_method_name이 없으면(호출부가 안 넘겼거나 신고 자체가 없음) 판정하지
    않는다 — "모른다"를 "위반"으로 단정하면 안 되기 때문(이 프로젝트 전반의
    원칙, mart_views.sql의 UNKNOWN 판정과 동일).
    """
    if packing_group != PACKING_GROUP_HIGH_RISK or not unload_method_name:
        return None
    if unload_method_name in APPROVED_METHODS_FOR_HIGH_RISK_PACKING:
        return None
    return {
        "packing_group": packing_group,
        "unload_method_name": unload_method_name,
        "reason": (
            f"용기등급 {packing_group}(고위험) 화물은 전용 이송"
            f"({'/'.join(sorted(APPROVED_METHODS_FOR_HIGH_RISK_PACKING))})이 필요하나 "
            f"'{unload_method_name}' 하역으로 신고됨"
        ),
    }


def compute_packing_floor(violation: dict | None) -> RiskLevel:
    return RiskLevel.DANGER if violation else RiskLevel.SAFE
