"""그래프 탐색 결과에 대한 결정적(rule-based) 위험등급 하한(floor) 계산.

LLM이 근거 없이 등급을 낮춰버리는 것을 막기 위한 안전장치다. LLM 최종 판단은
이 floor보다 높은 등급은 줄 수 있어도 낮출 수는 없다 (service.py에서 강제 보정).

기상 임계값(강풍/고파랑) 판정은 기획서상 기상분석 에이전트의 책임 범위라 이번
안전관제 에이전트의 rule engine에는 포함하지 않는다.
"""

from enum import Enum

from .schemas import RiskLevel, max_risk_level

# 이 두 카테고리가 동시에 걸리면(예: 산화제 + 가연성물질) 단순 혼재금지를 넘어
# 화재·폭발로 직결될 수 있는 조합으로 보고 배정불가로 판정한다.
#
# [2026-09-16] ★ 이제 발동한다. 151종 적재 후 "산화제" 카테고리에
# IS_CLASSIFIED_AS 멤버가 3종 생겼다(기피 선언 화물은 7종). 2-hop 양쪽이
# 채워져 충돌이 실제로 생성된다.
#   전수 실측(22,650 순서쌍): MSDS 충돌 2건 이상인 쌍 94개 → 전부 배정불가.
#   compute_risk_floor의 BLOCKED 분기가 36종 시절 0건에서 94건이 됐다.
#
# [2026-08-23 과거 기록] 36종 시절에는 "산화제" 소속 화물이 0종이라 이 조건이
# 발동할 수 없었고, 그 사실을 여기 적어 두었다. 데이터가 늘면서 해소됐다 —
# "규칙은 맞는데 데이터가 없어 동작하지 않는" 상태와 "규칙이 틀린" 상태를
# 구분해 기록해 두면 이렇게 나중에 확인할 수 있다.
SEVERE_CATEGORY_COMBO = {"산화제", "가연성물질"}


def compute_risk_floor(conflicts: list[dict]) -> RiskLevel:
    """MSDS 텍스트 마이닝 기반(INCOMPATIBLE_WITH) 충돌의 하한 등급.

    0건 -> 안전 / 1건 -> 위험 / 2건 이상 또는 산화제+가연성물질 -> 배정불가.

    ─────────────────────────────────────────────────────────────────────────
    [2026-09-16 실측 — 현재 유효] 151종 전체 순서쌍 22,650개 전수 계산
    (scripts/recompute_safety_matrix.py, LLM 미사용 경로 그대로 호출)

      등급 분포   주의 8,456(37.3%) · 위험 7,812(34.5%) · 안전 5,056(22.3%)
                  · 배정불가 1,326(5.9%)
      축별 관여   MSDS 텍스트 8,476(37.4%) · 벌크 1,962(8.7%)
                  · 판정불가 15,510(68.5%) · 용기등급 0
      배정불가 1,326건의 구성  벌크 충돌 1,254 / MSDS 충돌 664(중복 포함)

    → **이 함수의 MSDS 축이 실제로 작동한다.** 전체 쌍의 37.4%에서 충돌을 잡고,
      배정불가 1,326건 중 664건에 관여한다. 다만 **차단의 주력은 여전히 벌크
      축**이다(1,254 / 1,326 = 94.6%). 발표·문서에서 "MSDS 기반 혼재 판정"이라고
      단정하지 말 것 — 정확히는 "MSDS가 주의·위험을 만들고 벌크가 차단한다".

    [2026-08-23 실측 — 과거 기록, 더 이상 유효하지 않음]
      Neo4j Chemical이 36종이던 시절 순서쌍 1,260개 기준으로는 배정불가 두 분기가
      모두 도달 불가였고("화물쌍당 충돌이 전부 1건", "산화제 소속 화물 0종"),
      배정불가 62건이 전부 벌크 축에서 나왔다. 이후 화물이 151종으로, 벌크 매핑이
      46 CFR 150 원문 기반 121종으로, 비호환 그룹쌍이 125쌍으로 확장되면서
      위 결론이 뒤집혔다. 기록은 남겨 둔다 — 같은 로직이 데이터 규모에 따라
      어떻게 다르게 동작하는지 보여주는 사례이기 때문이다.
    ─────────────────────────────────────────────────────────────────────────
    """
    if not conflicts:
        return RiskLevel.SAFE

    categories = {c["category"] for c in conflicts}
    if len(conflicts) >= 2 or SEVERE_CATEGORY_COMBO.issubset(categories):
        return RiskLevel.BLOCKED

    return RiskLevel.DANGER


# IMDG Code Chapter 7.2 격리 코드 -> RiskLevel 매핑.
# ─────────────────────────────────────────────────────────────────────────────
# [2026-08-23 전면 개정] IMDG 축은 "거리 판정"이 아니라 "조합 판별"로만 쓴다.
#
# 무엇이 문제였나 — IMDG Code 7.2가 규정하는 실제 이격거리는 코드1 수평 3m,
# 코드2 갑판상 6m(내화·내액 격벽 시)~12m, 코드3 12m, 코드4 종방향 24m다.
# 그런데 이전 구현은 코드1을 300m, 코드2를 400m 임계로 잡아 원 규정의 100배·
# 67배로 확대해 놓고 그것을 IMDG 근거라고 표시했다. IMDG 논리를 그대로 따르면
# 6m만 넘으면 코드2는 충족인데, 399m를 "위험"으로 판정하면서 근거를 IMDG로
# 든 셈이라 인용과 결론이 서로 반대였다.
#
# 왜 스케일 조정으로 해결되지 않나 — IMDG의 3~24m는 "같은 선체 안에서 사고 시
# 상호작용을 막는" 기준이다(같은 격벽·같은 소방설비·같은 승조원 전제). 우리가
# 판단하려는 것은 "부두에 각각 접안한 서로 다른 두 선박"이고, 여기서 위험이
# 전파되는 경로는 증기운 확산·복사열·BLEVE로 IMDG 7.2가 다루는 물리가 아니다.
# IMDG에 부두 단위 규정이 "없는" 것이지 못 찾은 것이 아니다.
#
# 전수 실측(2026-08-23, 36종 순서쌍 1,260개 — 이 개정을 하게 만든 근거):
#   · 이전 구현에서 IMDG가 단독으로 등급을 올린 조합 304건 — 전부 코드2,
#     그중 300건이 안전→위험으로 두 단계를 건너뛰었다.
#   · 그 결과 '주의' 등급이 전체 1,260건 중 4건뿐이라 4단계 등급 체계가
#     사실상 안전/위험 2단계로 퇴화해 있었다(개정 후 308건).
#   · 400m에 이진 절벽이 있었다 — 399m는 위험 620건, 401m는 316건.
#     부두가 2m 더 떨어졌다고 304개 조합이 통째로 넘어갔다.
#   · 배정불가 62건은 개정 전후 동일하다. 이 그래프의 SEGREGATE 간선은
#     코드1·2뿐이라(코드3·4는 0개) IMDG 축은 애초에 배정을 막은 적이 없다.
#
# 재검증(2026-09-16, 151종 순서쌍 22,650개 — 개정 결론이 유지되는지 확인):
#   · IMDG 조회가 걸린 쌍은 1,894건(8.4%)이며, 전부 참고 정보로만 응답에 실린다.
#   · 등급 분포가 주의 37.3% / 위험 34.5%로, 개정 목적이었던 "4단계 체계 복원"이
#     확대된 데이터에서도 유지된다(개정 전 '주의'는 1,260건 중 4건이었다).
#   · 화물이 4배로 늘어도 IMDG를 판정에서 뺀 결론은 바뀌지 않는다.
#
# 그래서 IMDG가 권위 있게 말해주는 부분만 남긴다: "이 두 Class는 국제 규정상
# 격리가 요구되는 조합"이라는 사실 진술. 이것은 인용 가능하고 MSDS 텍스트
# 마이닝이 놓치는 조합을 실제로 잡아준다. 반면 "그러므로 부두는 N m 떨어져야
# 한다"는 IMDG가 말한 적 없는 우리 쪽 창작이므로 판정에서 뺀다.
#
# 코드별 강도 차등(1<2<3<4)도 두지 않는다 — 그 강도는 선내 배치 난이도지
# 부두 간 위험도가 아니라서, 부두 문맥으로 옮길 때 순서가 보존된다는 근거가
# 없다. 확정 위험 판정은 MSDS축(compute_risk_floor)과 벌크축
# (compute_bulk_compatibility_floor)이 담당한다.
#
# 거리(distance_m)는 계속 응답에 실려 나가므로(ImdgSegregationConflict) 화면에
# 표시해 관제사가 직접 판단할 수 있다 — 시스템이 판정하지 않을 뿐이다.
# 부두 간 이격의 정량적 근거(위험물 취급시설 안전거리·QRA 기반 이격 등)를
# 확보하면 IMDG와 별개의 축으로 새로 설계할 것.
# ─────────────────────────────────────────────────────────────────────────────

# ── 맥락 분리 (2026-08-23) ───────────────────────────────────────────────────
# IMDG 축을 쓸 수 있는 맥락과 쓸 수 없는 맥락이 다르므로 함수를 나눈다.
# 하나의 compute_imdg_floor를 두 경로가 공유하면 한쪽에 맞춘 조정이 다른 쪽까지
# 끌고 가버린다(실제로 그랬다 — 부두 인접 기준으로 등급을 낮추자 동일 선석
# 경고까지 같이 낮아졌다).
#
#   부두 인접(다른 선석·다른 선박)  -> compute_imdg_berth_adjacency_floor
#                                      : 항상 SAFE. IMDG 규율 대상이 아니다.
#   동일 공간(동일 선석 동시 재항)  -> compute_imdg_costowage_floor
#                                      : 격리코드 강도를 반영한다.
# ─────────────────────────────────────────────────────────────────────────────


def compute_imdg_berth_adjacency_floor(imdg_conflicts: list[dict]) -> RiskLevel:
    """부두 인접 판정에서의 IMDG 하한 — **항상 SAFE**(= 이 축으로 판정하지 않음).

    IMDG Code Ch.7.2는 단일 선박 내 적부 규정이고, IMO는 항만 구역을 별도
    문서(MSC.1/Circ.1216 Revised Recommendations on the Safe Transport of
    Dangerous Cargoes and Related Activities in Port Areas)로 분리해 두었다.
    부두와 부두 사이는 IMDG의 규율 대상이 아니므로 등급 근거로 쓰지 않는다.

    그럼 왜 함수를 남기나 — 호출부에서 "IMDG를 의도적으로 배제했다"는 사실이
    코드에 드러나야 하기 때문이다. 조회 자체는 계속 한다(imdg_conflicts /
    imdg_classes는 응답에 실려 관제사에게 참고 정보로 표시된다). 판정에서만
    뺀다. 나중에 부두 간 이격의 정량적 근거(위험물 취급시설 안전거리·QRA 등)를
    확보하면 IMDG가 아닌 **별도 축**으로 새로 만들 것이지, 이 함수를 되살리는
    방식이 아니다.
    """
    return RiskLevel.SAFE


# 동일 공간(혼재 적재·동일 선석 동시 취급) 맥락에서의 격리코드 → 등급.
# 이 맥락은 IMDG가 실제로 규율하는 상황이므로 코드 강도를 반영한다:
#   1 (away from)                     -> 주의     : 최소 이격(3m)만 요구
#   2 (separated from)                -> 위험     : 다른 격창 요구 — 같은 공간 취급은 규정 위반
#   3·4 (완전 격창/종방향 분리)        -> 배정불가 : 같은 공간에 둘 수 없다는 뜻
# 다만 1<2<3<4를 4단계 RiskLevel로 옮기는 것은 IMDG의 공식 환산표가 아니라
# 이 구현의 해석이다("코드가 높을수록 더 강한 물리적 분리를 요구한다"는
# 방향성만 규정에서 직접 나온다).
IMDG_COSTOWAGE_CODE_TO_RISK_LEVEL: dict[str, RiskLevel] = {
    "1": RiskLevel.CAUTION,
    "2": RiskLevel.DANGER,
    "3": RiskLevel.BLOCKED,
    "4": RiskLevel.BLOCKED,
}


def compute_imdg_costowage_floor(imdg_conflicts: list[dict]) -> RiskLevel:
    """동일 공간(혼재 적재·동일 선석 동시 취급)에서의 IMDG 하한 등급.

    여러 화물과 동시에 격리 규정이 걸리면 그중 가장 심각한 등급을 채택한다.
    거리(distance_m)는 보지 않는다 — 이 맥락의 판단은 "떨어져 있는가"가 아니라
    "같은 공간에 있는가"이고, 같은 공간에 있다는 것 자체가 이미 전제다.

    ★ 부두 인접 판정에 쓰지 말 것. 그 맥락은
      compute_imdg_berth_adjacency_floor가 담당한다(모듈 상단 주석 참고).

    한계 — 현재 유일한 호출부(berth_alerts)는 mart.berth_current_cargo를
    facility_name으로 묶으므로 "같은 부두에 동시에 있는 화물"이지 엄밀한
    "동일 선박 내 적부"는 아니다. IMDG가 규율하는 상황에 부두 인접보다 훨씬
    가깝지만 완전히 일치하지는 않는다. 동일 선박 단위로 좁히려면 같은 뷰의
    callsgn으로 묶으면 된다.
    """
    floor = RiskLevel.SAFE
    for conflict in imdg_conflicts:
        level = IMDG_COSTOWAGE_CODE_TO_RISK_LEVEL.get(
            conflict["segregation_code"], RiskLevel.SAFE
        )
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


# 2026-08-21 추가 — compute_imdg_costowage_floor는 SEGREGATE 관계가 있는 화물쌍만
# 본다. 두 화물의 IMDG Class가 둘 다 알려져 있는데도 SEGREGATE 관계가 없는
# 경우는 두 가지 서로 다른 상황이 구분 없이 섞여 있다: ① IMDG Chapter 7.2 표상
# 코드가 "X"(격리 불필요가 아니라 "개별 물질별로 확인" — 일반 규정 부재)인 경우,
# ② 이 Class 조합 자체가 로더 커버리지 밖이라 그래프에 없는 경우. 로더가 X를
# 관계로 적재하지 않으므로 둘 다 "SEGREGATE 없음"으로 동일하게 보이고, 이걸
# SAFE로 취급하면 "모르면 진행하지 않는다"는 프로젝트 전반 원칙(V-DG-01 등)에
# 위배된다. 두 화물의 Class가 모두 확인됐는데 공인 규정이 없다는 사실 자체는
# "위험하다"고 단정할 근거도 아니므로 BLOCKED까지는 올리지 않고, 최소
# 주의(CAUTION)로 격상해 "규정상 개별 확인이 필요한 조합"임을 노출한다.
#
# [2026-08-23] ★ 이 축도 IMDG 파생이므로 **동일 공간 맥락에서만** 쓴다.
# 부두 인접 판정에서는 쓰지 않는다 — IMDG가 부두 간에 적용되지 않는다면
# "그 IMDG 규정을 못 찾았다"는 사실도 부두 간 판정 근거가 될 수 없다.
# (compute_imdg_berth_adjacency_floor의 주석 참고)
def compute_imdg_unconfirmed_floor(has_unconfirmed_pair: bool) -> RiskLevel:
    return RiskLevel.CAUTION if has_unconfirmed_pair else RiskLevel.SAFE


# 2026-08-21 추가 — 벌크 액체화학물질 호환성 그룹(bulk_compatibility.py) 기반
# 참고 축. MSDS 텍스트 마이닝·IMDG 공인 격리표 두 축과 근거가 다른 세 번째
# 신호다(자세한 출처·적용범위 유의사항은 bulk_compatibility.py 모듈 docstring).
# compute_imdg_floor와 같은 모양으로, find_bulk_compatibility_conflict raw dict의
# 리스트를 받아 그중 가장 심각한 등급을 채택한다.
def compute_bulk_compatibility_floor(conflicts: list[dict]) -> RiskLevel:
    floor = RiskLevel.SAFE
    for c in conflicts:
        floor = max_risk_level(floor, c["risk_level"])
    return floor


# ─────────────────────────────────────────────────────────────────────────────
# 판정 가능성(assessability) — 2026-08-23 추가
#
# 문제: "혼재금지 충돌 0건"이 지금까지 무조건 안전으로 처리됐는데, 그 0건에는
# 성격이 전혀 다른 두 가지가 섞여 있다.
#   ① 양쪽 근거를 다 보고 겹치는 게 없었다        → 안전이 맞다
#   ② 애초에 볼 근거가 없어 아무것도 못 걸렀다     → 안전이 아니라 판정 불가
#
# 이 프로젝트는 다른 곳에서 이미 ②를 구분하고 있다. 기상분석은 관측이 없거나
# 오래되면 WorkStatus.UNKNOWN("판단불가")을 별도로 두고, 스케줄링은 depth_m이
# NULL인 선석을 후보에서 뺀다("모르면 추천하지 않는다"). 챗봇은 그래프 미등재
# 화물을 in_graph=False로 표시하고, 대시보드는 정체 미확인 화물을
# UNIDENTIFIED_CARGO 경고로 올린다. 안전관제만 이 원칙에서 빠져 있었다.
#
# 실측 근거(2026-08-23, 36종 1,260개 순서쌍 전수):
#   · 충돌 검출 354건 / 양방향 검사 후 무충돌 706건 /
#     단방향만 검사 가능 122건 / 양방향 모두 근거 없음 78건
#     → 현재 "안전" 906건 중 200건(22.1%)이 불완전한 검사 결과였다.
#   · 더 근본적으로, IncompatibleMaterial 12종 중 IS_CLASSIFIED_AS 멤버가 있는
#     것은 일부뿐이다. 멤버가 0인 카테고리(물/수분·열/점화원·중합반응물질 등)는
#     화물이 아니라 환경 조건이라 화물 대 화물 충돌을 만들 수 없다. 그 결과
#     36종 중 31종의 유효 기피 카테고리가 '산소/공기' 하나뿐이고 거기 속한
#     화물은 수소 1종이다 — 이 31종은 수소를 뺀 어떤 화물과도 구조적으로
#     충돌이 나올 수 없다.
#   · 원인은 KOSHA MSDS의 J08("피해야 할 물질")이 36종 중 27종에서 "자료없음"인
#     것이다. 2026-08-23 KOSHA API를 직접 호출해 원천을 확인했다 — 벤젠·가솔린·
#     톨루엔·아세톤·수소 모두 API 응답 자체가 "자료없음"이었다(대조군 황산은
#     정상 반환). 우리 수집 문제가 아니라 원문에 값이 없으므로 재수집으로는
#     해결되지 않는다.
#
# 등급을 CAUTION으로 두는 이유: 기상분석의 UNKNOWN은 심각도 최상위지만, 그건
# 관측치 하나가 없으면 그 시각 판정 전체가 불가능해서다. 여기는 화물쌍 단위이고
# 위 실측대로 200/1,260이 해당돼, 최상위로 두면 자동배정이 대량 차단된다.
# RiskLevel에 5번째 값을 넣는 방법도 있으나 그건 API 계약 변경이라 프론트까지
# 번진다. CAUTION은 경고로 노출하되 배정 동작(BLOCKED만 차단)은 건드리지 않는
# 절충점이다.
# ─────────────────────────────────────────────────────────────────────────────


class Assessability(str, Enum):
    """이 화물쌍의 혼재금지 판정에 쓸 근거가 얼마나 있었는가."""

    FULL = "양방향판정"      # 양쪽 다 근거를 갖고 있어 제대로 검사됨
    PARTIAL = "단방향판정"    # 한쪽 근거만 있어 절반만 검사됨
    NONE = "판정불가"        # 어느 방향으로도 검사할 근거가 없음


def compute_pair_assessability(
    *,
    target_avoids: set[str],
    target_classes: set[str],
    adjacent_avoids: set[str],
    adjacent_classes: set[str],
    live_categories: set[str],
) -> Assessability:
    """화물쌍 하나의 판정 가능성.

    2-hop 경로(A -[INCOMPATIBLE_WITH]-> 카테고리 <-[IS_CLASSIFIED_AS]- B)가
    성립하려면 방향마다 양 끝이 다 있어야 한다. 그리고 기피 카테고리는
    live_categories(= IS_CLASSIFIED_AS 멤버가 실재하는 카테고리)에 속해야
    의미가 있다 — 아무도 속하지 않은 카테고리를 기피한다는 사실은 화물 대 화물
    판정에 아무 기여도 하지 못한다.

    live_categories를 인자로 받는 이유: 그래프에서 매번 계산해야 로더 재적재로
    데이터가 늘었을 때 판정 범위도 같이 넓어진다(상수로 박으면 어긋난다).
    """
    forward = bool(target_avoids & live_categories) and bool(adjacent_classes)
    reverse = bool(adjacent_avoids & live_categories) and bool(target_classes)

    if forward and reverse:
        return Assessability.FULL
    if forward or reverse:
        return Assessability.PARTIAL
    return Assessability.NONE


def compute_assessability_floor(assessability: Assessability) -> RiskLevel:
    """판정 근거가 불완전하면 주의로 격상한다.

    충돌이 실제로 검출된 화물쌍에는 이 격상이 의미 없다(이미 위험 이상이므로
    max_risk_level에 흡수된다). 효과가 나타나는 곳은 "충돌 0건"으로 안전이 될
    뻔한 화물쌍이며, 바로 그게 이 함수의 목적이다.
    """
    return RiskLevel.SAFE if assessability is Assessability.FULL else RiskLevel.CAUTION
