"""ChatResponse(내부) → Citation 목록(외부 계약) 변환.

`POST /api/v1/rag/query`의 응답에는 내부 3계층 구조(벡터 청크 / 정형 컬럼 / 그래프
관계)를 노출하지 않는다. 클라이언트에게 필요한 건 "이 답변의 근거가 무엇이고 어디서
왔는가"이지 "백엔드가 어떤 저장소를 썼는가"가 아니다. 계층이 늘어도 이 형태는 그대로다.

세 계층 모두 결국 MSDS 섹션에서 유래하므로 `section`/`section_name`을 채울 수 있다:
    인화점        ← detail09 I14        용기등급   ← detail14 N08
    노출기준      ← detail08 H0202      EMS       ← detail14 N1202/N1204
    신호어        ← detail02 B0404      IMDG 등급  ← detail14 N06
    GHS 분류      ← detail02 B02        혼재금지   ← detail10 J코드
(detail14는 청크 대상에서 제외했을 뿐 섹션 자체는 존재한다.)

`score`가 None이면 확정값이라는 뜻이다. 벡터 청크만 유사도를 갖는다.
"""

from __future__ import annotations

import re

from .schemas import ChatResponse, ChemicalProfile, Citation

# 정형 컬럼 → (표시명, 유래 섹션, 섹션명). msds_chemical의 컬럼 주석 및 Alembic 0007과
# 같은 매핑이어야 한다.
_COLUMN_ORIGIN: list[tuple[str, str, str, str]] = [
    ("flash_point_text", "인화점", "detail09", "물리화학적 특성"),
    ("boiling_point_text", "끓는점", "detail09", "물리화학적 특성"),
    ("vapor_pressure_text", "증기압", "detail09", "물리화학적 특성"),
    ("specific_gravity_text", "비중", "detail09", "물리화학적 특성"),
    ("exposure_limit_kr", "국내 노출기준", "detail08", "노출방지 및 개인보호구"),
    ("signal_word", "신호어", "detail02", "유해성·위험성"),
    ("packing_group", "용기등급", "detail14", "운송에 필요한 정보"),
    ("ems_fire", "EMS 화재시 비상조치", "detail14", "운송에 필요한 정보"),
    ("ems_spill", "EMS 유출시 비상조치", "detail14", "운송에 필요한 정보"),
]

# 그래프 관계 → (프로필 필드, 표시명, 유래 섹션, 섹션명)
_GRAPH_ORIGIN: list[tuple[str, str, str, str]] = [
    ("hazard_classes", "GHS 유해성 분류", "detail02", "유해성·위험성"),
    ("incompatible_categories", "혼재금지 물질 카테고리", "detail10", "안정성 및 반응성"),
    ("imdg_classes", "IMDG Class", "detail14", "운송에 필요한 정보"),
]

_PUNCT = re.compile(r"[\s:,·/()\[\]{}~×－–—-]+")

# 이보다 짧은 값은 답변에 우연히 등장할 확률이 높아 인용 판정에서 제외한다.
# 실제 오탐 사례: 신호어 "위험"(2자)이 "…두는 것은 위험합니다"에 걸려, 답변이 인용한
# 적 없는 신호어가 근거로 붙었다. 화물이 둘이면 같은 값이 두 번 붙기까지 했다.
_MIN_MATCH_LEN = 4


def _core(value: str) -> str:
    """KOSHA 값에서 출처 꼬리를 떼고 핵심만 남긴다.

    '-11 ℃|   ※출처 : ICSC' → '-11 ℃'
    """
    return value.split("|")[0].split("※")[0].strip()


def _norm(text: str) -> str:
    """비교용 정규화 — 공백·구두점을 지운다. LLM이 '-11 ℃'를 '-11℃'로,
    '인화성 액체 : 구분2'를 '인화성 액체 구분2'로 바꿔 써도 매칭되게 하기 위함이다."""
    return _PUNCT.sub("", text)


def _mentioned(value: str, answer_norm: str) -> bool:
    core = _norm(_core(value))
    return len(core) >= _MIN_MATCH_LEN and core in answer_norm


def _structured_citations(profile: ChemicalProfile, answer_norm: str) -> list[Citation]:
    """답변이 실제로 인용한 확정값만 citation으로 만든다.

    프로필의 모든 값을 싣지 않는 이유: 화물 하나에 컬럼 9개 + 그래프 3종이라 질문과
    무관한 근거가 화면을 덮는다. 답변 본문에 등장한 값만 고르면 "이 문장의 근거가
    이것"이라는 대조가 성립한다.

    한계 — LLM이 값을 완전히 바꿔 쓰면(예: '-11℃'를 '영하 11도') 놓친다. 근거를
    과다 표기하는 것보다 누락하는 편이 오해가 적다고 보고 이 방향을 택했다.
    벡터 청크는 이 필터를 적용하지 않는다(검색 결과 자체가 근거이므로).
    """
    name = profile.name_ko or profile.name_en or profile.chem_id
    out: list[Citation] = []

    for field, label, section, section_name in _COLUMN_ORIGIN:
        value = getattr(profile, field, None)
        if value and _mentioned(value, answer_norm):
            out.append(Citation(
                chem_name=name, cas_no=profile.cas_no,
                section=section, section_name=section_name,
                text=f"{label}: {_core(value)}", score=None,
            ))

    for field, label, section, section_name in _GRAPH_ORIGIN:
        values = getattr(profile, field, None) or []
        hit = [v for v in values if _mentioned(v, answer_norm)]
        if hit:
            out.append(Citation(
                chem_name=name, cas_no=profile.cas_no,
                section=section, section_name=section_name,
                text=f"{label}: {', '.join(hit)}", score=None,
            ))

    return out


def build_citations(response: ChatResponse) -> list[Citation]:
    """벡터 청크(유사도 있음) + 답변이 인용한 확정값(유사도 None) 순으로 반환한다."""
    citations = [
        Citation(
            chem_name=c.chemical_name,
            cas_no=c.cas_no,
            section=c.section_key,
            section_name=c.section_label,
            text=c.content,
            score=c.score,
        )
        for c in response.retrieved_chunks
    ]

    answer_norm = _norm(response.answer)
    for profile in response.graph_evidence.profiles:
        citations.extend(_structured_citations(profile, answer_norm))

    return citations
