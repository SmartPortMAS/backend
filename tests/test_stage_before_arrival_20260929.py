"""AIS 항해상태가 없는 입항 전 배의 시점 (2026-09-29).

입항 목록에서 [판정 요청]한 배는 아직 AIS 에 잡히지 않아 항해상태가 없다. 예전엔 시점이
None 이 되어 '입항전'으로 기록되면서도 조치안·받는 곳이 비었다(운영 308 HYODONG CHEMI).
판정 대상이 PORT-MIS 배정 선석이면 아직 붙지 않은 배이므로 입항 전으로 본다.
"""
from app.models.assessment_history import AssessmentLevel, AssessmentStage
from app.services.assessment import _action_for, _RECIPIENT_BY_STAGE, stage_from_nav_status


def test_no_ais_with_portmis_target_is_before_arrival():
    assert stage_from_nav_status(None, target_source="PORT-MIS") is AssessmentStage.BEFORE_ARRIVAL
    assert stage_from_nav_status("  ", target_source="PORT-MIS") is AssessmentStage.BEFORE_ARRIVAL


def test_no_ais_without_portmis_target_stays_unknown():
    # AIS 로 접안한 배인데 상태가 없거나, 대상 출처를 모르면 여전히 모른다.
    assert stage_from_nav_status(None) is None
    assert stage_from_nav_status(None, target_source="AIS") is None


def test_nav_status_wins_over_target_source():
    assert stage_from_nav_status("정박(계류)", target_source="PORT-MIS") is AssessmentStage.DURING_CARGO


def test_unfit_before_arrival_gets_action_and_berth_operator():
    stage = stage_from_nav_status(None, target_source="PORT-MIS")
    action = _action_for(AssessmentLevel.UNFIT, stage)
    assert action is not None and action.value == "대체선석"
    assert _RECIPIENT_BY_STAGE[stage].value == "선석운영주체"
