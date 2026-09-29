"""관제사 관점 점검 (2026-09-29) — 경고 문구가 안전 에이전트 판정과 어긋나던 곳."""
import pytest

from app.agents.safety import berth_alerts
from app.agents.chatbot.schemas import Confidence
from app.agents.chatbot.service import _operational_confidence
from app.agents.safety.service import _drop_generic_checklist


@pytest.mark.parametrize("found_any, has_assessment, insufficient, expected", [
    (False, True, True, Confidence.MEDIUM),   # 9/29 실측 — 가상의 배 + 규칙 판정: '낮음'이던 것
    (False, True, False, Confidence.HIGH),
    (True, False, False, Confidence.HIGH),
    (True, False, True, Confidence.LOW),
    (False, False, False, Confidence.LOW),
])
def test_operational_confidence_respects_rule_verdict(found_any, has_assessment, insufficient, expected):
    assert _operational_confidence(
        found_any=found_any, has_assessment=has_assessment, data_insufficient=insufficient,
    ) is expected


def test_generic_checklist_items_are_dropped():
    # 9/29 실측 가솔린 체크리스트 — 프롬프트로 금지해도 일반 수칙이 절반이었다
    items = [
        "환기 상태를 점검하여 증기 농도가 위험 수치에 이르지 않도록 하시오.",
        "탱크와 연결 호스의 접지가 제대로 이루어졌는지 확인하시오.",
        "작업 중 금연 및 열점화원 제거 상태를 확인하시오.",
        "인화점 -40℃ 이므로 하역 중 점화원 관리를 확인하시오.",       # 숫자가 있으면 남긴다
        "가솔린·케로젠 ↔ 옆 부두 질산 혼재금지 — 격리 거리를 확인하시오.",  # 화물 이름이 있으면 남긴다
    ]
    assert _drop_generic_checklist(items, ["가솔린", "케로젠"]) == [items[1], items[3], items[4]]

_A, _B = "000001", "000002"


def _rows(ship_of_b: str) -> list[dict]:
    return [
        {"facility_name": "UTT부두", "callsgn": "SHIP1", "chem_id": _A, "cargo_name": "1,2-디아미노에탄",
         "imdg_class": None, "wharf_name": None},
        {"facility_name": "UTT부두", "callsgn": ship_of_b, "chem_id": _B, "cargo_name": "2-에틸헥실 아크릴산",
         "imdg_class": None, "wharf_name": None},
    ]


@pytest.fixture
def msds_conflict(monkeypatch):
    # A ↔ B MSDS 혼재금지 1건 — 그래프 조회만 바꿔 끼운다(판정 함수는 그대로)
    async def fake_facts(driver, sem, target_id, other_ids):
        other = _B if target_id == _A else _A
        return ([{"chem_id": other, "category": "가연성물질"}], [], set(), [], {}, (set(), set()))

    monkeypatch.setattr(berth_alerts, "_segregation_facts", fake_facts)


@pytest.mark.asyncio
async def test_same_ship_pair_is_caution_isolation_check(msds_conflict):
    # GOLDEN DENISE 재현 — 한 배가 실은 두 화물이 '→ 배정불가'로 올라, 판정 '주의'와 나란히 떴다
    [alert] = await berth_alerts._segregation_alerts(_rows("SHIP1"), None)
    assert alert["risk_level"] == "주의" and alert["level"] == "WARNING"
    assert "같은 선박 적재 — 격리 적재 확인 필요" in alert["message"]


@pytest.mark.asyncio
async def test_two_ships_pair_keeps_rule_engine_level(msds_conflict):
    [alert] = await berth_alerts._segregation_alerts(_rows("SHIP2"), None)
    assert alert["risk_level"] != "주의" and "같은 선박" not in alert["message"]
