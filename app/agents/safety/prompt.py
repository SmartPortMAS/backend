from .msds_context import RELEVANT_SECTIONS
from .schemas import ImdgSegregationConflict, IncompatibleConflict, PackagingViolation, RiskLevel

SYSTEM_PROMPT = """\
당신은 울산항 액체화물 하역 안전관제를 보조하는 AI입니다.
아래 규칙을 반드시 지켜 JSON으로만 응답하세요.

1. risk_level은 "안전" < "주의" < "위험" < "배정불가" 순으로 심각도가 높아집니다.
   입력으로 주어지는 "규칙엔진 하한(rule_engine_floor)"보다 낮은 등급을 선택해서는
   안 됩니다. 하한보다 높게 판단할 근거가 있다면 그렇게 해도 됩니다.
2. checklist와 key_hazards는 반드시 입력으로 제공된 MSDS 발췌문 안의 내용에
   근거해서만 작성하세요. 제공되지 않은 사실을 추측해서 만들어내지 마세요.
3. checklist는 5~8개, key_hazards는 2~5개의 짧고 실행 가능한 한국어 항목으로 작성하세요.
4. reasoning은 관제 담당자가 근거를 바로 이해할 수 있도록 1~2문단으로 요약하세요.
   혼재금지 충돌이 있다면 어떤 화물과 어떤 카테고리 때문인지 명시하세요.
5. "IMDG 공인 격리 충돌"은 화물 개별 반응성이 아니라 국제해사기구(IMO)가 정한
   위험물 대분류(Class) 간 공인 규정(IMDG Code Chapter 7.2)에 근거합니다.
   "혼재금지 충돌"(MSDS 텍스트 기반)보다 근거가 더 확실하니, IMDG 충돌이 있다면
   reasoning에서 반드시 그 사실과 격리 코드를 명시하세요.
6. "포장·하역방식 부적합"은 인접 화물과 무관하게 대상 화물 자신의 신고 내용만
   보는 판정입니다(용기등급 대비 하역방식). 이게 있다면 인접 선석 충돌과는
   별개 문제로 명확히 구분해서 reasoning에 적으세요.
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


def _format_imdg_conflicts(conflicts: list[ImdgSegregationConflict]) -> str:
    if not conflicts:
        return "(IMDG 공인 일반 격리표 기준 충돌 없음)"

    lines = []
    for c in conflicts:
        lines.append(
            f"  - 선석 '{c.adjacent_berth}'의 화물 '{c.adjacent_name}'({c.adjacent_chem_id}, "
            f"IMDG Class {c.adjacent_imdg_class})와 격리 코드 {c.segregation_code} "
            f"(대상 화물 IMDG Class {c.target_imdg_class})"
        )
    return "\n".join(lines)


def _format_packaging_violations(violations: list[PackagingViolation]) -> str:
    if not violations:
        return "(포장·하역방식 부적합 없음 — 또는 하역방식이 신고되지 않아 판정 안 함)"

    return "\n".join(f"  - {v.reason}" for v in violations)


def build_user_prompt(
    *,
    target_cargo_name: str,
    hazard_summary: dict[str, list[str]],
    conflicts: list[IncompatibleConflict],
    imdg_conflicts: list[ImdgSegregationConflict],
    packaging_violations: list[PackagingViolation],
    rule_engine_floor: RiskLevel,
) -> str:
    return f"""\
[대상 화물]
{target_cargo_name}

[대상 화물 MSDS 발췌]
{_format_hazard_summary(hazard_summary)}

[인접 선석 혼재금지 충돌 (Neo4j 그래프 탐색 결과, MSDS 텍스트 기반)]
{_format_conflicts(conflicts)}

[인접 선석 IMDG 공인 격리 충돌 (IMDG Code Chapter 7.2 일반 격리표 기반)]
{_format_imdg_conflicts(imdg_conflicts)}

[포장·하역방식 부적합 (대상 화물 자신의 신고 내용 기준)]
{_format_packaging_violations(packaging_violations)}

[규칙엔진 하한 등급 (rule_engine_floor)]
{rule_engine_floor.value}

위 정보를 바탕으로 risk_level, checklist, key_hazards, reasoning을 JSON으로 응답하세요.
포장·하역방식 부적합이 있다면 reasoning과 checklist에 그 내용을 반드시 반영하세요.
"""
