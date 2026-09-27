from .msds_context import RELEVANT_SECTIONS
from .schemas import (
    BulkCompatibilityConflict,
    IncompatibleConflict,
    PackagingViolation,
    RiskLevel,
    UnassessedPair,
)

SYSTEM_PROMPT = """\
당신은 울산항 액체화물 하역 안전관제를 보조하는 AI입니다.
아래 규칙을 반드시 지켜 JSON으로만 응답하세요.

1. ★ 위험등급은 이미 규칙엔진이 확정했습니다. 당신은 등급을 정하지 않습니다.
   입력의 "확정 위험등급"을 사실로 받아들이고, 그 등급이 왜 그렇게 나왔는지를
   제공된 근거로 설명하세요. 등급을 다르게 판단하거나 "더 위험해 보인다"고
   쓰지 마세요 — 화면에 표시되는 등급과 당신의 설명이 어긋나면 관제사가
   무엇을 믿어야 할지 알 수 없게 됩니다.
2. checklist와 key_hazards는 반드시 입력으로 제공된 MSDS 발췌문 안의 내용에
   근거해서만 작성하세요. 제공되지 않은 사실을 추측해서 만들어내지 마세요.
3. checklist는 5~8개, key_hazards는 2~5개의 짧고 실행 가능한 한국어 항목으로 작성하세요.
4. reasoning은 1문단으로 대상 화물의 위험성(MSDS 발췌 근거)을 설명하고, 충돌이나
   판정 근거 부족이 있으면 어떤 화물과 어떤 카테고리·그룹 때문인지 적으세요.
   ★ "어떤 검사를 했고 충돌이 없었다"는 요약과 등급 설명은 쓰지 마세요 — 코드가
   확정 문장으로 앞에 붙입니다. 입력에 없는 조치·이행 상황("조치가 이행 중")도 쓰지 마세요.
5. "포장·하역방식 부적합"은 인접 화물과 무관하게 대상 화물 자신의 신고 내용만
   보는 판정입니다(용기등급 대비 하역방식). 이게 있다면 인접 선석 충돌과는
   별개 문제로 명확히 구분해서 reasoning에 적으세요.
6. "벌크 액체화학물질 호환성 그룹 충돌"은 미국 해안경비대 규정 46 CFR Part 150의
   산적 액체화물 호환성 차트(인접 탱크 기준)와 그 예외표에서 옮긴 것입니다.
   reasoning에서 "IMDG Code"나 "국내 법령"인 것처럼 표현하지 말고, "46 CFR Part 150
   호환성 차트 기준"이라고 정확히 표현하세요.
7. "혼재금지 판정 근거 부족" 항목에 화물이 실려 있으면, 그 화물에 대한
   "충돌 없음"은 **안전이 확인된 것이 아니라 확인할 자료가 없는 것**입니다.
   reasoning에서 "충돌이 없어 안전하다"고 쓰지 말고 "판정 근거가 없어 확인이
   필요하다"고 정확히 구분해 쓰고, checklist에 자료 보완·전문가 확인 항목을
   하나 넣으세요. 근거가 없는 것을 안전으로 단정하지 않는 것이 이 시스템의
   원칙입니다.
8. 이 판정은 **서로 다른 부두에 접안한 선박 사이**의 문제입니다. IMDG Code의
   격리 규정은 단일 선박 내 화물 적부 기준(이격거리 3~24m)이라 부두 간에는
   적용 대상이 아니므로, IMDG 격리코드를 부두 간 배치의 근거로 인용하지
   마세요. 위 입력에도 IMDG 항목은 제공되지 않습니다.
9. 입력에 "검사하지 않음" 또는 "인접 화물 없음"이라고 적힌 항목은 **검증된 것이
   아닙니다.** "검증이 이루어졌다", "적합하게 수행되고 있다", "충돌 없음으로 확인됐다"처럼
   쓰지 말고, 하지 않은 검사는 하지 않았다고 그대로 쓰세요.
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
    rule_engine_floor: RiskLevel,
    adjacent_count: int,
    unload_method_name: str | None,
) -> str:
    no_neighbors = adjacent_count == 0
    return f"""\
[대상 화물]
{target_cargo_name}

[대상 화물 MSDS 발췌]
{_format_hazard_summary(hazard_summary)}

[인접 선석 혼재금지 충돌 (Neo4j 그래프 탐색 결과, MSDS 텍스트 기반)]
{_NO_NEIGHBORS if no_neighbors else _format_conflicts(conflicts)}

[벌크 액체화학물질 호환성 그룹 충돌 (46 CFR Part 150 호환성 차트 기준)]
{_NO_NEIGHBORS if no_neighbors else _format_bulk_compatibility(bulk_compatibility_conflicts)}

[포장·하역방식 부적합 (대상 화물 자신의 신고 내용 기준)]
{_format_packaging_violations(packaging_violations, unload_method_name)}

[혼재금지 판정 근거 부족 — ★ 위의 "충돌 없음"을 "안전 확인"으로 읽으면 안 되는 화물]
{_format_unassessed(unassessed_pairs, adjacent_count)}

[확정 위험등급 — 규칙엔진이 결정했으며 변경 대상이 아님]
{rule_engine_floor.value}

위 정보를 바탕으로 checklist, key_hazards, reasoning을 JSON으로 응답하세요.
reasoning은 "확정 위험등급"이 그렇게 나온 이유를 제공된 근거로 설명하는 글입니다.
포장·하역방식 부적합이 있다면 reasoning과 checklist에 그 내용을 반드시 반영하세요.
"""
