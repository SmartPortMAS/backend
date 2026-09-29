"""2026-09-26 판정 잡 LLM 생략 회귀 테스트.

watch_arrivals 는 10분마다 같은 배를 다시 판정한다(게이트 인터락이 변화를 제때 받아야 해서).
기록되는 건 결과가 바뀔 때뿐인데 LLM 은 매 회차 불렀다. 규칙 판정으로 변화를 먼저 가리고,
바뀌었을 때만 LLM 을 붙인다. DB 를 쓰는 테스트는 로컬 개발 DB 에 쓰고 롤백한다.
"""

from datetime import datetime, timezone

import pytest

from app.agents.orchestrator.schemas import LLMSummary, OrchestratorResult, OverallDecision
from app.agents.safety.schemas import LLMAssessment
from app.agents.weather.schemas import ObservationFactor, WeatherAssessmentResult, WorkStatus
from app.jobs import arrival_watcher
from app.llm.null_client import NullLLMClient
from app.models.assessment_history import AssessmentStage
from app.services.assessment import is_unchanged_from_last, record_from_orchestrator

NOW = datetime.now(timezone.utc)
CALL_SIGN = "TESTSKIP0926"


def _result(*, evidence_missing: bool) -> OrchestratorResult:
    """검증모드 '적합선석없음'. evidence_missing 이면 판정불가, 아니면 부적합으로 기록된다."""
    obs = ObservationFactor(value=None, unit="m/s", is_stale=True)
    weather = WeatherAssessmentResult(
        status=WorkStatus.NORMAL, assessed_at_utc=NOW, wind=obs, wave=obs, reasons=["테스트"]
    )
    return OrchestratorResult(
        overall_decision=OverallDecision.NO_ELIGIBLE_BERTH,
        weather_assessment=weather,
        evidence_missing=evidence_missing,
        assignment_trace=["테스트 선석"],
        summary="테스트",
    )


@pytest.mark.asyncio
async def test_null_llm_fills_every_schema_without_calling_out():
    llm = NullLLMClient()
    summary = await llm.generate_structured(system_prompt="", user_prompt="", schema=LLMSummary)
    assessment = await llm.generate_structured(system_prompt="", user_prompt="", schema=LLMAssessment)
    assert isinstance(summary, LLMSummary) and summary.summary == ""
    assert isinstance(assessment, LLMAssessment) and assessment.checklist == []


@pytest.mark.asyncio
async def test_unchanged_matches_record_rule(db):
    stage = AssessmentStage.DURING_CARGO
    await record_from_orchestrator(
        db, call_sign=CALL_SIGN, vessel_name=None, stage=stage, wharf_name="테스트",
        result=_result(evidence_missing=True),
    )
    await db.flush()  # 판정 잡은 회차마다 커밋한다 — 직전 기록이 보이는 상태를 만든다
    assert await is_unchanged_from_last(db, call_sign=CALL_SIGN, stage=stage,
                                        result=_result(evidence_missing=True)) is True
    # 등급이 바뀌면(판정불가 → 부적합) 변화다
    assert await is_unchanged_from_last(db, call_sign=CALL_SIGN, stage=stage,
                                        result=_result(evidence_missing=False)) is False
    # 단계가 바뀌어도 변화다
    assert await is_unchanged_from_last(db, call_sign=CALL_SIGN, stage=AssessmentStage.BEFORE_ARRIVAL,
                                        result=_result(evidence_missing=True)) is False


@pytest.mark.asyncio
async def test_first_assessment_is_a_change(db):
    assert await is_unchanged_from_last(db, call_sign=CALL_SIGN + "X", stage=AssessmentStage.DURING_CARGO,
                                        result=_result(evidence_missing=True)) is False


@pytest.mark.asyncio
async def test_llm_only_called_when_result_changes(db, monkeypatch):
    stage = AssessmentStage.DURING_CARGO
    real_llm = object()
    used: list = []

    async def fake_orchestrate(_db, _driver, llm_client, _request, evidence_missing):
        used.append(llm_client)
        return _result(evidence_missing=evidence_missing)

    await record_from_orchestrator(
        db, call_sign=CALL_SIGN, vessel_name=None, stage=stage, wharf_name="테스트",
        result=_result(evidence_missing=True),
    )
    await db.flush()  # 판정 잡은 회차마다 커밋한다 — 직전 기록이 보이는 상태를 만든다

    # 결과가 같다 → 규칙 판정 한 번, LLM 없음, None
    monkeypatch.setattr(arrival_watcher, "orchestrate",
                        lambda d, dr, l, r, **kw: fake_orchestrate(d, dr, l, r, True))
    got = await arrival_watcher.judge_with_llm_on_change(
        db, None, real_llm, None, call_sign=CALL_SIGN, stage=stage)
    assert got is None
    assert len(used) == 1 and isinstance(used[0], NullLLMClient)

    # 결과가 바뀌었다 → 규칙 판정 뒤 실제 LLM 으로 다시
    used.clear()
    monkeypatch.setattr(arrival_watcher, "orchestrate",
                        lambda d, dr, l, r, **kw: fake_orchestrate(d, dr, l, r, False))
    got = await arrival_watcher.judge_with_llm_on_change(
        db, None, real_llm, None, call_sign=CALL_SIGN, stage=stage)
    assert got is not None and got.evidence_missing is False
    assert len(used) == 2 and isinstance(used[0], NullLLMClient) and used[1] is real_llm
