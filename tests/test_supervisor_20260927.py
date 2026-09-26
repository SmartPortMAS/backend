"""감독자 — 도구 의견과 교차 확인 (27번 설계안 B단계, 2026-09-27).

DB·그래프 없이 도구 계층을 바꿔치기해 오케스트레이터의 흐름만 본다.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.orchestrator import service as orch
from app.agents.orchestrator.schemas import LLMSummary, OrchestratorRequest, OverallDecision
from app.agents.safety.schemas import CargoRef, RiskLevel, SafetyAssessmentResult
from app.agents.scheduling.schemas import BerthCandidate, OccupancyStatus, VesselSpec
from app.agents.tools import BerthCheck
from app.agents.weather.schemas import (
    ForecastWarning,
    ObservationFactor,
    WeatherAssessmentResult,
    WorkStatus,
)
from app.models.assessment_history import AssessmentLevel
from app.services.assessment import level_from_decision

NOW = datetime.now(timezone.utc)


class _SummaryLLM:
    async def generate_structured(self, *, schema, **_):
        return schema(**{name: "" for name in schema.model_fields}) if schema is LLMSummary else None


def _request() -> OrchestratorRequest:
    return OrchestratorRequest(
        vessel=VesselSpec(draught_m=9.0, call_sign="TEST"),
        cargo=CargoRef(chem_id="001008"),
        window_start=NOW, window_end=NOW + timedelta(hours=24),
        assigned_wharf_name="효성부두",
    )


def _weather(forecast: ForecastWarning | None) -> WeatherAssessmentResult:
    obs = ObservationFactor(value=5.0, unit="m/s", is_stale=False)
    return WeatherAssessmentResult(
        status=WorkStatus.NORMAL, assessed_at_utc=NOW, wind=obs, wave=obs,
        reasons=["정상"], forecast_warning=forecast,
    )


def _patch(monkeypatch, forecast):
    async def fake_berth(*a, **k):
        return BerthCheck(candidate=BerthCandidate(
            rank=1, berth_id="효성부두", wharf_name="효성부두", port_name="온산항", depth_m=12.0,
            draught_margin_m=3.0, occupancy_status=OccupancyStatus.AVAILABLE,
        ))

    async def fake_weather(db, **k):
        return _weather(forecast)

    async def fake_segregation(*a, **k):
        return SafetyAssessmentResult.model_construct(
            target_cargo_name="벤젠", risk_level=RiskLevel.SAFE, rule_engine_floor=RiskLevel.SAFE,
            conflicts=[], bulk_compatibility_conflicts=[], packaging_violations=[],
            unassessed_pairs=[], cargo_verdicts=[], checklist=[], key_hazards=[], reasoning="",
        )

    monkeypatch.setattr(orch.tools, "check_berth", fake_berth)
    monkeypatch.setattr(orch.tools, "check_weather", fake_weather)
    monkeypatch.setattr(orch.tools, "check_segregation", fake_segregation)


@pytest.mark.asyncio
async def test_forecast_deterioration_is_conditional_fit(monkeypatch):
    # 25번 점검 C4 재현 — 6시간 뒤 하역중단 예보를 넣어도 아무 표시 없이 '적합'이던 문제
    worse_at = NOW + timedelta(hours=6)
    _patch(monkeypatch, ForecastWarning(
        window_end_utc=NOW + timedelta(hours=24), forecast_points_checked=8,
        will_deteriorate=True, worst_status=WorkStatus.STOP,
        earliest_deterioration_at_utc=worse_at, points=[],
    ))
    result = await orch.orchestrate(None, None, _SummaryLLM(), _request())

    assert result.overall_decision is OverallDecision.APPROVED
    assert result.conditions and "하역중단" in result.conditions[0]
    assert result.condition_key == "예보:하역중단"   # 시각은 키에 넣지 않는다
    level, headline = level_from_decision(result)
    assert level is AssessmentLevel.FIT and "조건부" in headline
    assert [o.axis for o in result.opinions] == ["선석", "기상", "혼재"]


@pytest.mark.asyncio
async def test_no_forecast_deterioration_has_no_condition(monkeypatch):
    _patch(monkeypatch, ForecastWarning(
        window_end_utc=NOW + timedelta(hours=24), forecast_points_checked=8,
        will_deteriorate=False, worst_status=WorkStatus.NORMAL, points=[],
    ))
    result = await orch.orchestrate(None, None, _SummaryLLM(), _request())

    assert result.conditions == [] and result.condition_key == ""
    level, headline = level_from_decision(result)
    assert level is AssessmentLevel.FIT and "조건부" not in headline
