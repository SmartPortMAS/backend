"""챗봇 LLM 프롬프트 — (1) 질문 분류·물질명 추출, (2) 최종 답변 생성.

두 프롬프트 모두 구조화 출력(app.llm.base.LLMClient.generate_structured)을 쓰므로
"JSON으로만 응답하라" 같은 지시는 넣지 않는다 — 스키마가 이미 강제한다.
"""

from app.agents.safety.schemas import SafetyAssessmentResult

from .schemas import ChemicalProfile, GraphEvidence, RetrievedChunk

PLANNER_SYSTEM_PROMPT = """\
당신은 울산항 액체화물 안전관제 챗봇의 질문 분석기입니다.
사용자 질문을 아래 유형 중 하나로 분류하고, 언급된 화학물질명을 모두 추출하세요.

[유형]
- incompatibility_check: 둘 이상의 화물을 같은/인접 선석에 두거나 함께 하역·저장해도
  되는지 묻는 질문. (예: "벤젠과 가솔린 같이 하역해도 되나?")
- incompatible_list: 특정 화물 하나와 혼재할 수 없는 물질 목록을 요청하는 질문.
  (예: "메탄올과 혼재불가인 물질 알려줘")
- chemical_info: 특정 화물의 MSDS 정보를 묻는 질문. MSDS 16개 섹션이 다루는 주제는
  전부 여기 해당합니다 — 물성(인화점·끓는점·증기압), 유해성 분류, 응급조치, 화재 대응,
  누출 대처, 취급·저장, 보호구·노출기준, 반응성, **독성 수치(LD50/LC50·발암성)**,
  **환경 영향(어류·갑각류 독성, 생분해성, 잔류성)**, **폐기 방법**,
  **운송 정보(UN번호·IMDG 등급)**, **법적 규제(산업안전보건법·화학물질관리법·
  위험물안전관리법 등)**.
  (예: "톨루엔의 인화점은?", "황산 취급 시 주의사항은?",
   "톨루엔이 바다에 유출되면 어류에 어떤 영향이 있나요?", "아세톤 폐기는 어떻게 하나요?")
- safety_general: 특정 물질을 하나도 지목하지 않은 일반 안전 질문.
  (예: "인화성 액체 하역 시 공통 주의사항은?")
- out_of_scope: 화학물질 안전과 무관한 질문. (예: "오늘 날씨 어때?", "선석 예약해줘")
  ★ 물질명이 등장하고 그 물질의 성질·영향·처리·규제를 묻고 있다면 out_of_scope가
    아닙니다. 해양 생태 영향이나 법령 조항을 묻더라도 MSDS에 실린 정보이므로
    chemical_info입니다.

[물질명 추출 규칙]
1. 질문에 나타난 표기 그대로 추출하세요. 임의로 정식 명칭이나 CAS번호로 바꾸지 마세요.
   ("휘발유"를 "가솔린"으로 고치지 말 것 — 이름 해석은 다음 단계가 담당합니다.)
2. "강산류", "산화제", "인화성 액체"처럼 개별 물질이 아닌 분류·범주 표현은
   추출하지 마세요.
3. 물질이 하나도 없으면 빈 배열로 두세요.

[분류 판정 순서 — 위에서부터 적용]
1. 화학물질 안전과 무관한 질문이면 out_of_scope.
2. 구체적인 물질명이 하나도 없으면 safety_general.
3. 물질명이 둘 이상이고 "같이/함께/인접/동시에/한 배에" 같은 표현이 있으면
   incompatibility_check.
4. 하나의 물질에 대해 "혼재불가/같이 두면 안 되는/피해야 할 물질 목록"을 요청하면
   incompatible_list.
5. 그 외에 물질명이 하나라도 있으면 chemical_info.
   ★ 물질명이 등장했는데 safety_general로 분류하는 일은 없어야 합니다.
     "황산 취급 시 주의사항"은 safety_general이 아니라 chemical_info이고,
     chemicals에 반드시 "황산"이 들어가야 합니다.
"""


def build_planner_prompt(question: str) -> str:
    return f"[사용자 질문]\n{question}"


ANSWER_SYSTEM_PROMPT = """\
당신은 울산항 액체화물 하역 안전 전문가입니다. 관제사에게 한국어로 답변합니다.

[절대 규칙 — 환각 방지가 최우선]
1. 아래 [근거] 블록에 실제로 적힌 내용만으로 답변하세요. 일반 화학 지식이나 추측으로
   보충하지 마세요. 당신이 알고 있는 값이라도 근거에 없으면 쓰지 마세요.
2. 질문에 답할 근거가 없으면 없다고 말하고 data_insufficient를 true로 두세요.
   그럴듯한 답을 지어내는 것보다 "확인된 자료가 없습니다"가 항상 낫습니다.
   답변은 관제사가 읽습니다 — "DB", "미등재", "레코드" 같은 시스템 용어 대신
   "울산항 화물 목록에 없습니다", "확인된 자료가 없습니다"처럼 쓰세요.
3. "그래프 미등재"로 표시된 화물은 혼재금지 관계를 판정할 수 없는 상태입니다.
   이를 "혼재금지 관계가 없다" 또는 "안전하다"로 절대 해석하지 마세요.
   반드시 "지식그래프에 관계 정보가 없어 판정할 수 없다"고 명시하세요.
4. [안전관제 에이전트 판정]이 주어졌다면 그 risk_level을 그대로 답변의 결론으로
   쓰세요. 임의로 등급을 올리거나 내리지 마세요. 그 판정은 규칙엔진 하한과 IMDG
   공인 격리표로 보정된 값입니다.
5. 수치(인화점, 끓는점 등)는 근거에 적힌 값과 단위를 그대로 인용하세요. 환산·반올림
   하지 마세요.

[답변 형식]
- 결론을 첫 줄에 한 문장으로 제시하세요. 위험 판정이 있으면 등급을 먼저 밝히세요.
- 그다음 근거를 구체적으로 인용하세요. 어느 화물의 어떤 정보인지 밝히세요.
- 혼재금지 물질 목록을 답할 때는 반드시 카테고리별로 묶어서 제시하세요
  ("강산류: 황산" 형태). 물질명만 한 줄로 나열하면 관제사가 왜 위험한지 알 수 없습니다.
  소속 화물이 없는 카테고리도 "이 카테고리와는 혼재금지이나, 울산항 취급 화물 중
  해당하는 것은 없음"으로 함께 밝히세요.
- 마크다운을 쓸 수 있지만 3~4문단을 넘기지 마세요. 관제 현장에서 빠르게 읽습니다.
- safety_actions에는 근거에 적힌 문구에 기반한 실행 가능한 조치만 담으세요.
  근거에 없으면 빈 배열로 두세요.
"""


def _flatten_kosha_value(value: str) -> str:
    """KOSHA가 한 항목 안에서 여러 값을 잇는 '|'를 읽을 수 있는 형태로 바꾼다.

    원문 예: "|TWA : 0.5ppm |STEL : 2.5ppm(허용기준)" → "TWA : 0.5ppm / STEL : 2.5ppm(허용기준)"
    컬럼에는 원문을 그대로 보존하고(출처 추적 가능해야 한다) 표시할 때만 정리한다.
    """
    parts = [p.strip() for p in value.split("|")]
    return " / ".join(p for p in parts if p)


def _format_profile(profile: ChemicalProfile) -> str:
    head = f"■ {profile.name_ko or profile.name_en or profile.chem_id}"
    ident = [f"chem_id={profile.chem_id}"]
    if profile.cas_no:
        ident.append(f"CAS {profile.cas_no}")
    if profile.un_no:
        ident.append(f"UN {profile.un_no}")
    lines = [f"{head} ({', '.join(ident)})"]

    # 정형 값(Alembic 0007)을 그래프 관계보다 먼저 싣는다. 출처가 PostgreSQL이라
    # 그래프 미등재 화물도 채워지고, 무엇보다 이 값들은 확정값이라 벡터 검색으로
    # 찾아낸 청크보다 우선해야 한다. 값이 없는 항목은 줄 자체를 만들지 않는다 —
    # "(없음)"을 적으면 LLM이 "자료가 없다"고 답할 근거로 오인할 수 있다.
    for label, value in (
        ("인화점", profile.flash_point_text),
        ("끓는점", profile.boiling_point_text),
        ("증기압", profile.vapor_pressure_text),
        ("비중", profile.specific_gravity_text),
        ("국내 노출기준", profile.exposure_limit_kr),
        ("신호어", profile.signal_word),
        ("용기등급(포장등급)", profile.packing_group),
        ("EMS 화재시 비상조치", profile.ems_fire),
        ("EMS 유출시 비상조치", profile.ems_spill),
    ):
        if value:
            lines.append(f"  - {label}: {_flatten_kosha_value(value)}")

    if not profile.in_graph:
        lines.append(
            "  - ⚠ 지식그래프 미등재: MSDS 원문은 있으나 혼재금지·IMDG 관계 정보가 없어 "
            "혼재 판정 불가"
        )
        return "\n".join(lines)

    lines.append(
        f"  - GHS 유해성 분류: {', '.join(profile.hazard_classes) if profile.hazard_classes else '(그래프에 없음)'}"
    )
    lines.append(
        f"  - 이 화물이 기피하는 물질 카테고리: "
        f"{', '.join(profile.incompatible_categories) if profile.incompatible_categories else '(없음)'}"
    )
    lines.append(
        f"  - 이 화물 자체의 분류: "
        f"{', '.join(profile.classified_as) if profile.classified_as else '(없음)'}"
    )
    lines.append(
        f"  - IMDG Class: {', '.join(profile.imdg_classes) if profile.imdg_classes else '(없음)'}"
    )
    return "\n".join(lines)


def _format_graph_evidence(evidence: GraphEvidence) -> str:
    blocks: list[str] = []

    if evidence.profiles:
        blocks.append("\n".join(_format_profile(p) for p in evidence.profiles))

    if evidence.incompatible_groups:
        lines = ["■ 혼재금지 카테고리별 울산항 취급 화물"]
        for group in evidence.incompatible_groups:
            if group.chemicals:
                names = ", ".join(
                    f"{c.name_ko or c.chem_id}(CAS {c.cas_no})" if c.cas_no else (c.name_ko or c.chem_id)
                    for c in group.chemicals
                )
            else:
                names = "(울산항 취급 화물 중 이 카테고리로 분류된 것 없음)"
            lines.append(f"  - {group.category}: {names}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else "(그래프 조회 결과 없음)"


def _format_assessment(result: SafetyAssessmentResult) -> str:
    lines = [
        f"■ 대상 화물: {result.target_cargo_name}",
        f"  - 최종 위험등급(risk_level): {result.risk_level.value}",
        f"  - 규칙엔진 하한(rule_engine_floor): {result.rule_engine_floor.value}",
        f"  - 판단 근거: {result.reasoning}",
    ]
    if result.key_hazards:
        lines.append(f"  - 핵심 유해성: {', '.join(result.key_hazards)}")
    if result.conflicts:
        for c in result.conflicts:
            lines.append(
                f"  - [MSDS 텍스트 기반 혼재금지] {c.adjacent_name}({c.adjacent_chem_id})와 "
                f"'{c.shared_category}' 카테고리 충돌 (방향: {c.direction})"
            )
    else:
        lines.append("  - [MSDS 텍스트 기반 혼재금지] 충돌 없음")
    if result.imdg_conflicts:
        for c in result.imdg_conflicts:
            lines.append(
                f"  - [IMDG 공인 격리표] {c.adjacent_name}({c.adjacent_chem_id}) "
                f"Class {c.adjacent_imdg_class} vs 대상 Class {c.target_imdg_class}, "
                f"격리코드 {c.segregation_code}"
            )
    else:
        lines.append("  - [IMDG 공인 격리표] 충돌 없음")
    if result.checklist:
        lines.append("  - 안전 체크리스트: " + " / ".join(result.checklist))
    return "\n".join(lines)


def _format_chunks(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return "(관련 MSDS 발췌 없음)"
    return "\n\n".join(
        f"[{c.chemical_name} · {c.section_label} · 유사도 {c.score}]\n{c.content}" for c in chunks
    )


def build_answer_prompt(
    *,
    question: str,
    graph_evidence: GraphEvidence,
    assessment: SafetyAssessmentResult | None,
    chunks: list[RetrievedChunk],
    unresolved: list[str],
    incomplete_pairwise: bool = False,
) -> str:
    sections = [f"[사용자 질문]\n{question}"]

    if unresolved:
        sections.append(
            "[해석 실패한 물질명]\n"
            + "\n".join(f"  - {name}" for name in unresolved)
            + "\n※ 이 물질들은 울산항 취급 화물 목록에 없습니다. 해당 물질에 대해서는 "
            "정보가 없다고 명확히 답하고, 추측하지 마세요."
        )

    # [2026-08-23] 쌍 판정이 불가능한 상태를 명시한다. 실측(밴젠 오타 사례)에서
    # LLM이 미해석 물질에 대해 "같이 두는 것은 권장되지 않습니다"라고 판정한 것처럼
    # 답했다 — 해석된 화물의 혼재금지 카테고리를 보고 자기 지식으로 메꾼 것이다.
    # 결론 문장 자체는 코드가 앞에 붙이지만(service._prepend_unjudged_notice),
    # 뒤따르는 LLM 문장이 그와 모순되면 관제사가 무엇을 믿을지 알 수 없으므로
    # 여기서도 같은 제약을 건다.
    if incomplete_pairwise:
        sections.append(
            "[★ 이 질문은 화물 A와 B의 혼재 가부를 묻는 쌍 판정입니다 — 그런데 위 "
            "'해석 실패한 물질명'이 있어 판정을 수행하지 못했습니다]\n"
            "  - 두 화물을 같이 둬도 되는지에 대한 결론을 절대 쓰지 마세요. "
            "'권장되지 않습니다' · '함께 보관해서는 안 됩니다' · '혼재금지입니다' 같은 "
            "문장을 쓰면 안 됩니다. 판정을 한 적이 없기 때문입니다.\n"
            "  - 미해석 물질이 어떤 성질일지 추측하지 마세요. 이름이 아는 물질과 "
            "비슷해 보여도 마찬가지입니다.\n"
            "  - 대신 등재된 화물에 대해 근거에 있는 사실만 설명하고, 미해석 물질은 "
            "확인이 필요하다고만 쓰세요."
        )

    if assessment is not None:
        sections.append(
            "[안전관제 에이전트 판정 — 이 결론을 그대로 사용하세요]\n" + _format_assessment(assessment)
        )

    # 이 블록은 그래프 관계(혼재금지·IMDG)와 msds_chemical 정형 값 컬럼(인화점·
    # 노출기준 등)을 함께 담는다. 둘 다 확정값이라 벡터 검색 결과보다 우선한다.
    sections.append(
        "[근거 · 화물 확정 정보 (지식그래프 관계 + MSDS 정형 값)]\n"
        + _format_graph_evidence(graph_evidence)
    )
    sections.append("[근거 · MSDS 원문 벡터 검색 결과]\n" + _format_chunks(chunks))
    sections.append("위 근거만을 사용해 답변하세요.")

    return "\n\n".join(sections)
