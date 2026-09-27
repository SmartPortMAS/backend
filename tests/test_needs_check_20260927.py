"""혼재 등급이 판정 등급으로 옮겨지는 적합 경로 (2026-09-27).

'확인요청' 등급은 없앴다 — 충돌 없이 근거만 없는 혼재 '주의'도 '주의'로 기록한다.
DB·그래프 없이 level_from_decision 의 적합 경로만 확인한다.
"""
from types import SimpleNamespace

from app.agents.orchestrator.schemas import OverallDecision
from app.agents.safety.schemas import RiskLevel
from app.agents.weather.schemas import WorkStatus
from app.models.assessment_history import AssessmentLevel
from app.services.assessment import level_from_decision


def _pair(name):
    return SimpleNamespace(adjacent_chem_id=name, adjacent_name=name, reason="MSDS 근거 없음")


def _result(conditions=(), **safety):
    base = dict(
        risk_level=RiskLevel.CAUTION, rule_engine_floor=RiskLevel.CAUTION,
        unassessed_pairs=[_pair("쌍1"), _pair("쌍2")], conflicts=[], bulk_compatibility_conflicts=[],
        packaging_violations=[], cargo_verdicts=[],
    )
    base.update(safety)
    return SimpleNamespace(
        overall_decision=OverallDecision.APPROVED,
        weather_assessment=SimpleNamespace(status=WorkStatus.NORMAL),
        safety_assessment=SimpleNamespace(**base),
        conditions=list(conditions),
    )


def test_unassessed_only_is_caution():
    level, _ = level_from_decision(_result())
    assert level is AssessmentLevel.CAUTION


def test_needs_check_level_removed():
    assert "확인요청" not in {lv.value for lv in AssessmentLevel}


def test_real_conflict_stays_caution():
    level, _ = level_from_decision(_result(
        risk_level=RiskLevel.DANGER, rule_engine_floor=RiskLevel.DANGER,
        conflicts=[SimpleNamespace(adjacent_name="황산", shared_category="가연성물질")],
    ))
    assert level is AssessmentLevel.CAUTION


def test_bulk_caution_stays_caution():
    level, _ = level_from_decision(_result(
        bulk_compatibility_conflicts=[SimpleNamespace(adjacent_name="가스", reason="특수가스")],
    ))
    assert level is AssessmentLevel.CAUTION


def test_no_neighbors_is_fit():
    level, _ = level_from_decision(_result(
        risk_level=RiskLevel.SAFE, rule_engine_floor=RiskLevel.SAFE, unassessed_pairs=[],
    ))
    assert level is AssessmentLevel.FIT


# ── 교차 확인 조건: 적합 그대로, 머리말에 조건부 (27번 설계안 B단계, 결정 7-1) ──

def test_condition_keeps_fit_and_marks_headline():
    level, headline = level_from_decision(_result(
        conditions=["체류 중 09/27 18:00 부터 '하역중단' 예보"],
        risk_level=RiskLevel.SAFE, rule_engine_floor=RiskLevel.SAFE, unassessed_pairs=[],
    ))
    assert level is AssessmentLevel.FIT
    assert "조건부" in headline
