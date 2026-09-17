"""중합성(안정화 필요) 화물 — 규칙 기반 체크리스트 항목.

IMDG 위험물 목록에서 정식 운송 명칭에 "STABILIZED"가 붙는 화물은 억제제
(inhibitor)로 중합 반응을 막은 상태로 운송된다. 억제제가 소진되거나 탱크 온도가
오르면 자기 가속 중합으로 폭발할 수 있다 — 2019-09-28 울산 염포부두 스톨트
그로이란드호 폭발은 항해 중 스티렌 모노머 탱크 온도가 이미 주의 단계를 넘은
것을 확인하지 않아 일어났다.

이 시스템은 선내 탱크 온도 데이터를 받지 못한다. 그래서 사고를 "탐지"한다고
말하지 않는다. 할 수 있는 것은 입항 전에 이 화물을 식별해 세이프티 미팅에서
확인할 항목을 **항상** 체크리스트 맨 앞에 올리는 것까지다 — LLM 이 MSDS 발췌에서
이 항목을 뽑을지 말지에 맡기지 않는다.

대상은 data-pipeline/data_pipeline/reference/imdg_dgl.py 의 ", STABILIZED" 항목 중
우리 화물 목록(msds_chemical)에 있는 세 종이다.
"""

# UN번호 -> 표시명 (imdg_dgl.py 의 정식 명칭 기준)
STABILIZED_UN_NUMBERS: dict[str, str] = {
    "1010": "1,3-부타디엔(안정화)",
    "1093": "아크릴로니트릴(안정화)",
    "2055": "스티렌 모노머(안정화)",
}
# UN번호가 비어 있는 행을 위한 CAS 보조 키
STABILIZED_CAS_NUMBERS: dict[str, str] = {
    "106-99-0": "1010",
    "107-13-1": "1093",
    "100-42-5": "2055",
}

CHECKLIST_ITEMS: tuple[str, ...] = (
    "중합성 화물 — 항해 중 화물 탱크 온도 기록을 하역 개시 전에 확인하고, 상승 추세면 개시를 보류하고 보고",
    "중합 억제제(inhibitor) 첨가 확인서의 잔량·유효기간 확인",
)


def stabilized_un_number(un_no: str | None, cas_no: str | None) -> str | None:
    """중합성 화물이면 UN번호, 아니면 None."""
    digits = "".join(ch for ch in str(un_no or "") if ch.isdigit())
    if digits in STABILIZED_UN_NUMBERS:
        return digits
    return STABILIZED_CAS_NUMBERS.get(str(cas_no or "").strip())


def with_stabilized_checklist(checklist: list[str], un_no: str | None, cas_no: str | None) -> list[str]:
    """중합성 화물이면 규칙 항목을 체크리스트 맨 앞에 붙인다(중복 없이)."""
    if stabilized_un_number(un_no, cas_no) is None:
        return list(checklist)
    head = [item for item in CHECKLIST_ITEMS if item not in checklist]
    return head + list(checklist)
