"""혼재 '주의'에서 '확인요청'(충돌 없음 · 근거 없음)을 가른다 (2026-09-27).

DB·그래프 없이 level_from_decision 의 적합 경로만 확인한다.
"""
from types import SimpleNamespace

from app.agents.orchestrator.schemas import OverallDecision
from app.agents.safety.schemas import RiskLevel
from app.agents.weather.schemas import WorkStatus
from app.models.assessment_history import GATE_BLOCKING_LEVELS, AssessmentLevel
from app.services.assessment import level_from_decision


def _result(**safety):
    base = dict(
        risk_level=RiskLevel.CAUTION, rule_engine_floor=RiskLevel.CAUTION,
        unassessed_pairs=["쌍1", "쌍2"], conflicts=[], bulk_compatibility_conflicts=[],
        packaging_violations=[],
    )
    base.update(safety)
    return SimpleNamespace(
        overall_decision=OverallDecision.APPROVED,
        weather_assessment=SimpleNamespace(status=WorkStatus.NORMAL),
        safety_assessment=SimpleNamespace(**base),
    )


def test_unassessed_only_is_needs_check():
    level, headline = level_from_decision(_result())
    assert level is AssessmentLevel.NEEDS_CHECK
    assert "2쌍" in headline


def test_needs_check_does_not_lock_gate():
    assert AssessmentLevel.NEEDS_CHECK not in GATE_BLOCKING_LEVELS


def test_real_conflict_stays_caution():
    level, _ = level_from_decision(_result(
        risk_level=RiskLevel.DANGER, rule_engine_floor=RiskLevel.DANGER, conflicts=["황산↔가연성물질"],
    ))
    assert level is AssessmentLevel.CAUTION


def test_bulk_caution_stays_caution():
    # 벌크 특수가스처럼 충돌 근거가 있는 주의는 확인요청이 아니다
    level, _ = level_from_decision(_result(bulk_compatibility_conflicts=["특수가스"]))
    assert level is AssessmentLevel.CAUTION


def test_no_neighbors_is_fit():
    level, _ = level_from_decision(_result(
        risk_level=RiskLevel.SAFE, rule_engine_floor=RiskLevel.SAFE, unassessed_pairs=[],
    ))
    assert level is AssessmentLevel.FIT

