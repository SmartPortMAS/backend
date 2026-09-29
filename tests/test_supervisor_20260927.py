"""감독자 — 도구 의견과 교차 확인 (27번 설계안 B단계, 2026-09-27).

DB·그래프 없이 도구 계층을 바꿔치기해 오케스트레이터의 흐름만 본다.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


def _patch(monkeypatch, forecast, **safety):
    async def fake_berth(*a, **k):
        return BerthCheck(candidate=BerthCandidate(
            rank=1, berth_id="효성부두", wharf_name="효성부두", port_name="온산항", depth_m=12.0,
            draught_margin_m=3.0, occupancy_status=OccupancyStatus.AVAILABLE,
        ))

    async def fake_weather(db, **k):
        return _weather(forecast)

    async def fake_segregation(*a, **k):
        return SafetyAssessmentResult.model_construct(**{**dict(
            target_cargo_name="벤젠", risk_level=RiskLevel.SAFE, rule_engine_floor=RiskLevel.SAFE,
            conflicts=[], bulk_compatibility_conflicts=[], packaging_violations=[],
            unassessed_pairs=[], cargo_verdicts=[], checklist=[], key_hazards=[], reasoning="",
            onboard_conflicts=[], adjacent_count=3, verdict_basis=["이웃 화물 3건과 MSDS·46 CFR 150 기준 충돌 없음"],
            needs_check=[], cargo_profile="",
        ), **safety})

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


# ── 2026-09-29 관제사용 반환 정리 ──────────────────────────────────────────────

class _EchoLLM:
    """받은 입력을 남기고 정해 둔 문장을 돌려준다."""

    def __init__(self, text: str):
        self.text, self.prompts = text, []

    async def generate_structured(self, *, user_prompt, schema, **_):
        self.prompts.append(user_prompt)
        return schema(summary=self.text)


@pytest.mark.asyncio
async def test_forecast_gap_is_needs_check_not_condition(monkeypatch):
    # ATHENS SPIRIT 재현 — 예보가 끝난 뒤를 '판단불가 예보(악화)'로 잡아 없는 경고가 붙었다
    gap_at = NOW + timedelta(hours=40)
    _patch(monkeypatch, ForecastWarning(
        window_end_utc=NOW + timedelta(hours=120), forecast_points_checked=40,
        will_deteriorate=False, worst_status=WorkStatus.NORMAL, no_forecast_after_utc=gap_at, points=[],
    ))
    result = await orch.orchestrate(None, None, _SummaryLLM(), _request())

    assert result.conditions == [] and result.level == "적합"
    assert any("예보가 아직 나오지 않았습니다" in m for m in result.needs_check)


@pytest.mark.asyncio
async def test_onboard_caution_level_and_basis(monkeypatch):
    # GOLDEN DENISE 재현 — 같은 선박 화물 충돌로 '주의'인데 콘솔은 초록 '적합', 혼재 근거는 비어 있었다
    _patch(monkeypatch, None, risk_level=RiskLevel.CAUTION, rule_engine_floor=RiskLevel.CAUTION,
           cargo_verdicts=[SimpleNamespace(risk_level=RiskLevel.SAFE)],   # 이웃 기준으로는 안전
           onboard_conflicts=[object()], verdict_basis=["같은 선박 화물끼리 혼재 충돌 1쌍(A↔B) — 격리 적재 확인 필요"])
    result = await orch.orchestrate(None, None, _SummaryLLM(), _request())

    assert result.overall_decision is OverallDecision.APPROVED   # 귀결 어휘는 그대로
    assert result.level == "주의" and "같은 선박" in result.headline
    seg = next(o for o in result.opinions if o.axis == "혼재")
    assert seg.level == "주의" and "같은 선박" in seg.evidence[0]


@pytest.mark.asyncio
async def test_empty_llm_summary_falls_back_to_template(monkeypatch):
    _patch(monkeypatch, None, needs_check=["하역방식 — 신고가 없어 포장·하역 적합성은 보지 못했습니다"])
    result = await orch.orchestrate(None, None, _SummaryLLM(), _request())

    assert result.summary == "이 선석이 이 선박·화물 조건에 맞습니다. 확인 필요: 하역방식."


@pytest.mark.asyncio
async def test_llm_prompt_carries_facts_only(monkeypatch):
    _patch(monkeypatch, None)
    llm = _EchoLLM("선석·기상·혼재 모두 기준 안입니다.")
    req = _request().model_copy(update={"target_source": "AIS", "reported_wharf_name": "정박지-M2"})
    result = await orch.orchestrate(None, None, llm, req)

    assert result.summary == "선석·기상·혼재 모두 기준 안입니다."
    assert result.headline.startswith("지금 접안한 선석이")
    assert result.assignment_trace[0].startswith("지금 접안한 '효성부두' 확인")
    assert "PORT-MIS 신고 계류시설: '정박지-M2'" in result.assignment_trace
    prompt = llm.prompts[0]
    # 비어 있는 칸은 넘기지 않는다 — 넘기면 "(해당 없음)"을 문장에 옮겨 적었다
    assert "[확인 필요]" not in prompt and "[조건]" not in prompt and "[대체 선석 제안]" not in prompt
    for gone in ("순위", "배정 경로", "탈락", "점유"):
        assert gone not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "최종 추천 선석인 효성부두는 수심과 흘수여유가 충분합니다.",   # 9/28 판정 기록에 실제로 남은 문장
    "효성부두 배정이 적합합니다.",
    "최적 선석으로 선정되었습니다.",
])
async def test_llm_summary_with_assignment_words_falls_back_to_template(monkeypatch, text):
    # 우리는 배정·추천을 하지 않는다(검증만) — 프롬프트로 금지해도 모델이 가끔 쓴다
    _patch(monkeypatch, None)
    result = await orch.orchestrate(None, None, _EchoLLM(text), _request())

    assert result.summary == "이 선석이 이 선박·화물 조건에 맞습니다."


@pytest.mark.asyncio
async def test_summarize_false_skips_summary_llm(monkeypatch):
    _patch(monkeypatch, None)
    llm = _EchoLLM("불리면 안 되는 문장")
    result = await orch.orchestrate(None, None, llm, _request(), summarize=False)

    assert llm.prompts == [] and result.summary == "이 선석이 이 선박·화물 조건에 맞습니다."


@pytest.mark.asyncio
async def test_unknown_weather_is_unknown_level_without_llm(monkeypatch):
    async def fake_berth(*a, **k):
        return BerthCheck(candidate=BerthCandidate(
            rank=1, berth_id="효성부두", wharf_name="효성부두", port_name="온산항", depth_m=12.0,
            draught_margin_m=3.0, occupancy_status=OccupancyStatus.AVAILABLE,
        ))

    async def stale_weather(db, **k):
        obs = ObservationFactor(value=2.5, unit="m/s", is_stale=True)
        return WeatherAssessmentResult(status=WorkStatus.UNKNOWN, assessed_at_utc=NOW, wind=obs, wave=obs,
                                       reasons=["풍속 관측치 없음 또는 기준 시각 대비 오래됨 - 판단 불가"])

    monkeypatch.setattr(orch.tools, "check_berth", fake_berth)
    monkeypatch.setattr(orch.tools, "check_weather", stale_weather)
    llm = _EchoLLM("불리면 안 되는 문장")
    result = await orch.orchestrate(None, None, llm, _request())

    # 예전엔 빨간 '기상불가_중단권고'로 보였다 — 근거 부족은 판정불가다
    assert result.level == "판정불가" and llm.prompts == []
    assert result.summary.endswith("빠진 근거를 채운 뒤 다시 판정해야 합니다.")
    assert "풍속 — 관측이 끊겼거나 오래됐습니다" in result.needs_check
