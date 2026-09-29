"""판정 기록을 입항 건(port_call_key)에 묶는다 (2026-09-29).

실측: 입항이 12시간 넘게 남은 배의 새 입항 건 옆에, 지난 입항 건(다른 화물)의 판정이 떴다(7척 전부).
"""
from app.api.v1.arrivals import _assessment_for
from app.models.assessment_history import AssessmentLevel
from app.services.assessment import _same_as_last

LAST = {"stage": "입항전", "level": "적합", "action": None, "suggested_key": "", "condition_key": "",
        "port_call_key": "DSPC6_2026_010"}


def test_new_port_call_is_recorded_even_if_level_same():
    assert _same_as_last(LAST, "입항전", AssessmentLevel.FIT, None, "", "", "DSPC6_2026_010")
    assert not _same_as_last(LAST, "입항전", AssessmentLevel.FIT, None, "", "", "DSPC6_2026_011")


def test_unknown_key_does_not_compare_port_call():
    assert _same_as_last(LAST, "입항전", AssessmentLevel.FIT, None, "", "", "")


def test_row_gets_only_its_own_port_call_assessment():
    old = {"level": "적합", "tag": "010"}
    by_key = {("DSPC6", "DSPC6_2026_010"): old}
    row_next = {"call_sign": "DSPC6", "port_call_key": "DSPC6_2026_011", "current_call_key": "DSPC6_2026_010"}
    assert _assessment_for(row_next, by_key) is None
    row_now = {**row_next, "port_call_key": "DSPC6_2026_010"}
    assert _assessment_for(row_now, by_key) is old


def test_legacy_record_without_key_only_for_current_call():
    legacy = {"level": "주의"}
    by_key = {("DSPC6", ""): legacy}
    assert _assessment_for({"call_sign": "DSPC6", "port_call_key": "DSPC6_2026_010",
                            "current_call_key": "DSPC6_2026_010"}, by_key) is legacy
    assert _assessment_for({"call_sign": "DSPC6", "port_call_key": "DSPC6_2026_011",
                            "current_call_key": "DSPC6_2026_010"}, by_key) is None
