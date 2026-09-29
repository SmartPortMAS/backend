from .msds_context import RELEVANT_SECTIONS
from .schemas import (
    BulkCompatibilityConflict,
    IncompatibleConflict,
    OnboardConflict,
    PackagingViolation,
    UnassessedPair,
)

# [2026-09-29] 등급 설명(reasoning)을 LLM 에서 걷어냈다. 예전 규칙 1("그 등급이 왜 그렇게
# 나왔는지 설명하라")과 규칙 4("등급 설명은 쓰지 마라")가 부딪혔고, 입력의 대부분이 MSDS
# 유해성이라 모델이 등급의 원인을 유해성에서 찾았다 — 같은 선박 화물 충돌로 '주의'가 된 배를
# "발암성 때문에 주의"라고 썼다(nano·mini 모두 재현). 등급의 이유는 코드(verdict_basis)가
# 확정하고, LLM 에는 등급을 주지도 묻지도 않는다.
SYSTEM_PROMPT = """\
당신은 울산항 액체화물 하역 관제 화면에 들어갈 화물 설명과 하역 전 확인 항목을 쓰는 보조자입니다.
JSON으로만 응답하세요. 등급과 그 이유는 규칙 엔진이 정해 화면에 따로 표시합니다 —
당신은 등급('안전'·'주의'·'위험'·'배정불가')을 말하거나 설명하지 않습니다.

1. cargo_profile — 이 화물이 하역 현장(부두·로딩암·탱크)에서 무엇이 위험한지 1~2문장, 100자 이내.
   [대상 화물 MSDS 발췌]만 근거로 쓰세요. 등급·충돌 여부·검사 여부는 쓰지 마세요.
   [대상 화물]이 여러 종('·'로 이어짐)이면 화물마다 나열하지 말고, 공통 위험과 특히 두드러진 화물을
   묶어 1~2문장, 150자 이내로 쓰세요. key_hazards·checklist 도 그 화물들 전체를 대상으로 고르세요.
2. key_hazards — 핵심 유해성 2~5개. 각각 15자 안팎의 명사구. MSDS 발췌만 근거로.
3. checklist — 하역 전에 관제사·터미널이 확인할 항목 3~5개, 각각 한 문장.
   - [이번 판정에서 확인할 것]에 항목이 있으면 그것부터 빠짐없이 확인 항목으로 옮기세요.
   - 나머지는 MSDS 발췌에서 이 화물의 하역 작업에 특히 필요한 것만 고르세요.
   - 이 화물은 탱커가 로딩암·호스로 탱크에 벌크 하역합니다. 용기·드럼·포장 취급을 전제로 한 항목은
     쓰지 마세요("용기 밀폐 상태 확인" 등).
   - 어느 화물에나 붙는 일반 수칙은 쓰지 마세요: 보호구 착용, MSDS 숙지, 폐기물 처리, 환기 확인,
     방폭 설비 확인, 스파크 없는 도구 사용, 점화원 제거 같은 문장. 이 화물만의 수치·조건
     (인화점·증기압·반응 상대·온도 관리 등)이 들어간 항목을 쓰세요.
4. 입력에 없는 사실·조치·이행 상황을 만들지 마세요. "검사하지 않음"·"인접 화물 없음"인 항목을
   검증된 것처럼 쓰지 마세요.
5. 46 CFR Part 150 호환성 차트 근거는 "46 CFR 150 호환성 차트"라고 쓰고 IMDG Code·국내 법령으로
   부르지 마세요. IMDG 격리 규정은 한 선박 안의 적부 기준이라 부두 간 배치의 근거가 아닙니다.
"""


def _format_hazard_summary(hazard_summary: dict[str, list[str]]) -> str:
    if not hazard_summary:
        return "(제공된 MSDS 발췌문 없음)"

    blocks = []
    for section_key, lines in hazard_summary.items():
        label = RELEVANT_SECTIONS.get(section_key, section_key)
        joined = "\n".join(f"  - {line}" for line in lines)
        blocks.append(f"[{label}]\n{joined}")
    return "\n\n".join(blocks)


def _format_conflicts(conflicts: list[IncompatibleConflict]) -> str:
    if not conflicts:
        return "(그래프 탐색 결과 인접 선석과의 혼재금지 충돌 없음)"

    lines = []
    for c in conflicts:
        lines.append(
            f"  - 선석 '{c.adjacent_berth}'의 화물 '{c.adjacent_name}'({c.adjacent_chem_id})와 "
            f"'{c.shared_category}' 카테고리 혼재금지 충돌 (방향: {c.direction})"
        )
    return "\n".join(lines)




def _format_bulk_compatibility(conflicts: list[BulkCompatibilityConflict]) -> str:
    if not conflicts:
        return "(벌크 액체화학물질 호환성 그룹 참고자료 기준 충돌 없음)"

    lines = []
    for c in conflicts:
        lines.append(
            f"  - 선석 '{c.adjacent_berth}'의 화물 '{c.adjacent_name}'({c.adjacent_chem_id}, "
            f"호환성그룹 {c.adjacent_group} {c.adjacent_group_name})와 대상 화물"
            f"(호환성그룹 {c.target_group} {c.target_group_name}) — {c.reason}"
        )
    return "\n".join(lines)


def _format_unassessed(pairs: list[UnassessedPair], adjacent_count: int) -> str:
    """판정 근거가 없었던 인접 화물을 프롬프트에 명시한다.

    이걸 안 적으면 LLM이 위쪽 "충돌 없음"을 "안전이 확인됨"으로 읽는다. 실제로는
    볼 근거가 없어 아무것도 걸러내지 못한 것이라, 두 상태를 구분해 주지 않으면
    답변이 근거보다 앞서 나간다.
    """
    if adjacent_count == 0:
        return _NO_NEIGHBORS
    if not pairs:
        return "(모든 인접 화물에 대해 판정 근거가 확보됨)"

    lines = []
    for p in pairs:
        lines.append(
            f"  - 선석 '{p.adjacent_berth}'의 화물 '{p.adjacent_name}'({p.adjacent_chem_id}): "
            f"{p.assessability} — {p.reason}"
        )
    return "\n".join(lines)


def _format_packaging_violations(
    violations: list[PackagingViolation], unload_method_name: str | None,
) -> str:
    # [2026-09-27] 예전엔 "부적합 없음 — 또는 판정 안 함"으로 넘겨 LLM 이 둘 중 하나를
    # 골랐다. 하역방식 없이 부른 판정에서 "포장·하역방식 검증이 이루어졌다"고 썼다(실측).
    # 어느 쪽인지는 코드가 안다 — 확정 문장으로 넘긴다.
    if violations:
        return "\n".join(f"  - {v.reason}" for v in violations)
    if not unload_method_name:
        return "(검사하지 않음 — 하역방식이 신고되지 않아 포장·하역방식 판정을 하지 않았습니다)"
    return f"(부적합 없음 — 신고된 하역방식 '{unload_method_name}' 기준)"


def _format_onboard(onboard: list[OnboardConflict]) -> str:
    if not onboard:
        return "(충돌 없음 — 또는 화물이 1종이라 비교 대상 없음)"
    return "\n".join(
        f"  - {c.cargo_a_name} ↔ {c.cargo_b_name}: {c.basis} {c.detail} "
        "(한 선박 적재 자체는 격리 시 허용 — 격리 적재 여부 확인 필요)"
        for c in onboard
    )


# [2026-09-27] 이웃이 0건이어도 예전엔 "충돌 없음"·"판정 근거가 확보됨"으로 넘겨, LLM 이
# "인접 선석 충돌 없음으로 확인"이라고 썼다. 비교할 이웃이 없었다는 사실을 그대로 적는다.
_NO_NEIGHBORS = "(인접 화물 없음 — 비교할 이웃 화물이 없어 혼재 검사를 하지 않았습니다)"


def build_user_prompt(
    *,
    target_cargo_name: str,
    hazard_summary: dict[str, list[str]],
    conflicts: list[IncompatibleConflict],
    bulk_compatibility_conflicts: list[BulkCompatibilityConflict],
    packaging_violations: list[PackagingViolation],
    unassessed_pairs: list[UnassessedPair],
    adjacent_count: int,
    unload_method_name: str | None,
    onboard_conflicts: list[OnboardConflict] | None = None,
    needs_check: list[str] | None = None,
    conflict_checks: list[str] | None = None,
) -> str:
    """conflict_checks — 배가 실은 **모든 화물**의 충돌 줄(화물 이름 포함, service._verdict_basis).

    [2026-09-29] 주면 대표 화물의 충돌 목록 대신 이것을 '확인할 것'에 쓴다. 예전엔 대표 화물 것만
    넘겨, 다른 화물의 충돌이 체크리스트에서 빠졌다. 아래 충돌 절(대표 화물 기준)은 그대로 둔다.
    """
    no_neighbors = adjacent_count == 0
    if conflict_checks is None:
        conflict_checks = (
            [f"이웃 {c.adjacent_berth} {c.adjacent_name}와 '{c.shared_category}' 혼재금지 충돌" for c in conflicts]
            + [f"이웃 {c.adjacent_berth} {c.adjacent_name}와 46 CFR 150 호환성 충돌 — {c.reason}"
               for c in bulk_compatibility_conflicts]
            + [f"포장·하역방식 부적합 — {v.reason}" for v in packaging_violations]
        )
    to_check = (
        [f"{c.cargo_a_name} ↔ {c.cargo_b_name} 같은 선박 적재 — 격리 적재 확인" for c in onboard_conflicts or []]
        + list(conflict_checks)
        + list(needs_check or [])
    )
    return f"""\
[대상 화물]
{target_cargo_name}

[대상 화물 MSDS 발췌]
{_format_hazard_summary(hazard_summary)}

[인접 선석 혼재금지 충돌 — 대상 화물 기준 (Neo4j 그래프 탐색 결과, MSDS 텍스트 기반)]
{_NO_NEIGHBORS if no_neighbors else _format_conflicts(conflicts)}

[벌크 액체화학물질 호환성 그룹 충돌 (46 CFR Part 150 호환성 차트 기준)]
{_NO_NEIGHBORS if no_neighbors else _format_bulk_compatibility(bulk_compatibility_conflicts)}

[포장·하역방식 부적합 (대상 화물 자신의 신고 내용 기준)]
{_format_packaging_violations(packaging_violations, unload_method_name)}

[혼재금지 판정 근거 부족 — ★ 위의 "충돌 없음"을 "안전 확인"으로 읽으면 안 되는 화물]
{_format_unassessed(unassessed_pairs, adjacent_count)}

[같은 선박 내 화물끼리의 혼재 충돌 (이 배가 함께 실은 화물 간)]
{_format_onboard(onboard_conflicts or [])}

[이번 판정에서 확인할 것]
{chr(10).join(f"  - {t}" for t in to_check) or "(없음)"}

위 정보를 바탕으로 cargo_profile, key_hazards, checklist를 JSON으로 응답하세요.
"""
