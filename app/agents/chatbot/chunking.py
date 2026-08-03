"""msds_payload(JSONB) → 임베딩 대상 텍스트 청크 변환.

적재(scripts/embed_msds.py)와 조회(retrieval.py)가 같은 규칙을 봐야 해서 별도
모듈로 뒀다. 특히 identity 청크의 문장 구성이 바뀌면 물질명 해석 품질이 바로
달라지므로, 여기를 고치면 전체 재적재가 필요하다.
"""

import hashlib
import html

from app.models import CHUNK_KIND_IDENTITY, CHUNK_KIND_SECTION, MsdsChemical

from .aliases import raw_aliases_for_cas

# KOSHA 원문에 HTML 엔티티가 그대로 들어있다. 청크에 남겨두면 LLM이
# "LD50 &gt;2000 ㎎/㎏"을 그대로 읽고 답변에도 그대로 인용한다.
#
# 두 번 이상 이스케이프된 값이 실제로 있다 — detail08 호흡기 보호의
# "산소가 부족한 경우(&amp;lt;19.6%)"는 한 번 풀면 "&lt;19.6%"에서 멈춘다.
# 그래서 더 이상 변하지 않을 때까지 반복한다. 상한을 두는 것은 병리적 입력에서
# 무한 루프에 빠지지 않게 하기 위함이고, 실측 최대는 2회다.
_MAX_UNESCAPE_PASSES = 4


def unescape(text: str) -> str:
    """HTML 엔티티를 원래 문자로 되돌린다(다중 이스케이프까지 처리)."""
    for _ in range(_MAX_UNESCAPE_PASSES):
        decoded = html.unescape(text)
        if decoded == text:
            return decoded
        text = decoded
    return text

# 챗봇 답변 근거로 임베딩할 MSDS 섹션.
#
# 16개 전부 넣으면 안 된다. 실측(34종 610청크 전량 임베딩, chem_ids 필터 적용,
# top_k=6, 19문항): 정답 섹션 1위 적중이 12섹션 13/19 → 16섹션 9/19로 떨어진다.
# 원인은 어휘 충돌이 아니라 "짧은 청크의 물질명 독식"이다 — detail01("제품명: 벤젠")
# 이나 detail03("물질명/CAS/함유량 100%")처럼 물질명 외에 내용이 없는 짧은 청크는
# 물질명이 들어간 질문이면 내용과 무관하게 상위를 먹는다(암모니아 혼재 질문에서
# detail03이 0.642로 1위, 정답 detail10은 top6 밖으로 밀림).
#
# 제외 섹션과 그 이유:
#   detail01 회사정보 — 제품명·연락처. 답변 근거 가치 없음
#   detail03 구성성분 — 물질명·이명·CAS는 identity 청크가 이미 담고,
#                       함유량은 34종 전부 "100%"라 정보량이 0
#   detail14 운송정보 — UN번호는 Chemical.un_no와 identity 청크에, IMDG 등급은
#                       (:Chemical)-[:HAS_IMDG_CLASS]->(:ImdgClass) 관계에 이미 있다.
#                       청크로 넣으면 같은 사실의 세 번째 사본이 되고, 게다가
#                       msds_preprocessor가 "운송정보보다 list_info가 더 안정적"이라며
#                       버린 열등한 소스라 그래프 값과 어긋날 수 있다
#   detail16 기타참고 — 자료 출처 목록. "ICSC(인화점)"처럼 항목명만 나열돼 있어
#                       값 없이 검색어만 갖춘 최악의 노이즈
#
# detail11(독성)과 detail12(환경)는 반드시 함께 넣어야 한다. LD50(실험동물)과
# LC50(어류) 문장 형태가 거의 같아, 하나만 있으면 그쪽이 상대 질문까지 가로챈다
# (+11만: "생분해·잔류성" 질문 1위를 detail11이 0.433으로 탈취. 둘 다 넣으면
# detail12가 0.491로 이긴다). 부분 도입이 미도입보다 나쁘다.
EMBEDDED_SECTIONS: dict[str, str] = {
    "detail02": "유해성·위험성",
    "detail04": "응급조치요령",
    "detail05": "폭발·화재시 대처방법",
    "detail06": "누출사고시 대처방법",
    "detail07": "취급 및 저장방법",
    "detail08": "노출방지 및 개인보호구",
    "detail09": "물리화학적 특성",
    "detail10": "안정성 및 반응성",
    "detail11": "독성에 관한 정보",
    "detail12": "환경에 미치는 영향",
    "detail13": "폐기시 주의사항",
    "detail15": "법적 규제현황",
}

_NULL_VALUES = frozenset({"자료없음", "해당없음", "-", "", "N/A", "없음"})

# 한 청크의 최대 길이(문자). 초과하면 항목 경계에서 분할한다.
# detail09(물리화학적 특성)는 항목이 20개가 넘어 한 덩어리로 두면 인화점 질문에
# 무관한 문장이 대량으로 딸려온다.
MAX_CHUNK_CHARS = 1200


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _display_name(row: MsdsChemical) -> str:
    return row.name_ko or row.name_en or row.chem_id


def _section_items(payload: dict, section_key: str) -> list[str]:
    """섹션의 itemDetail을 '항목명: 내용' 줄 목록으로 편다."""
    section = payload.get(section_key) or {}
    data = section.get("data") if isinstance(section, dict) else None
    if not isinstance(data, list):
        return []

    lines: list[str] = []
    for item in data:
        detail = unescape((item.get("itemDetail") or "").strip())
        if not detail or detail in _NULL_VALUES:
            continue
        label = unescape((item.get("msdsItemNameKor") or "").strip())
        # KOSHA는 한 항목 안에 여러 문장을 '|'로 이어 붙여 준다. 그대로 두면
        # 한 줄이 수백 자가 되므로 문장 단위로 편다.
        for part in detail.split("|"):
            cleaned = part.strip()
            if cleaned and cleaned not in _NULL_VALUES:
                lines.append(f"{label}: {cleaned}" if label else cleaned)
    return lines


def _pack(lines: list[str], header: str) -> list[str]:
    """줄 목록을 MAX_CHUNK_CHARS 이하 덩어리로 묶는다. 각 덩어리는 header로 시작한다."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = len(header)

    for line in lines:
        # 한 줄이 통째로 한도를 넘으면 쪼개지 않고 단독 청크로 둔다 — 문장 중간을
        # 자르면 검색 결과가 문맥 없는 조각이 되어 근거로 못 쓴다.
        if current and current_len + len(line) + 1 > MAX_CHUNK_CHARS:
            chunks.append(header + "\n".join(current))
            current, current_len = [], len(header)
        current.append(line)
        current_len += len(line) + 1

    if current:
        chunks.append(header + "\n".join(current))
    return chunks


def build_identity_chunk(row: MsdsChemical) -> str:
    """물질명 해석용 청크. 등재명·영문명·CAS·UN에 별칭 사전 항목을 함께 실어,
    사용자가 쓰는 통칭("휘발유", "벙커C")과 등재명("가솔린", "연료, 잔사유")의
    표현 격차를 임베딩 공간에서 좁힌다."""
    parts = [f"화학물질명: {_display_name(row)}"]
    if row.name_en:
        parts.append(f"영문명: {row.name_en}")
    if row.cas_no:
        parts.append(f"CAS번호: {row.cas_no}")
    if row.un_no:
        parts.append(f"UN번호: {row.un_no}")

    aliases = raw_aliases_for_cas(row.cas_no)
    if aliases:
        parts.append(f"이명·약칭: {', '.join(aliases)}")

    return "\n".join(parts)


def build_chunks(row: MsdsChemical) -> list[dict]:
    """한 화물의 전체 청크 목록을 반환한다.

    반환 항목: {chunk_kind, section_key, section_label, chunk_index, content}
    """
    chunks: list[dict] = [
        {
            "chunk_kind": CHUNK_KIND_IDENTITY,
            "section_key": "identity",
            "section_label": "물질 식별정보",
            "chunk_index": 0,
            "content": build_identity_chunk(row),
        }
    ]

    payload = row.msds_payload or {}
    name = _display_name(row)

    for section_key, label in EMBEDDED_SECTIONS.items():
        lines = _section_items(payload, section_key)
        if not lines:
            continue
        # 헤더에 물질명을 넣어야 검색 결과 청크만 봐도 어느 화물 얘기인지 알 수 있고,
        # "벤젠 인화점" 같은 질문에서 물질명 토큰이 유사도에 기여한다.
        header = f"[{name} - {label}]\n"
        for idx, content in enumerate(_pack(lines, header)):
            chunks.append(
                {
                    "chunk_kind": CHUNK_KIND_SECTION,
                    "section_key": section_key,
                    "section_label": label,
                    "chunk_index": idx,
                    "content": content,
                }
            )

    return chunks
