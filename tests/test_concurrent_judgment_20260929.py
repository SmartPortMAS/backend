"""판정 한 번의 대기 줄이기 (2026-09-29).

운영 Neo4j(Aura)는 조회 한 번이 약 300ms 라, 화물 6종 배의 근거 패널이 약 20초 걸렸다.
  · 화물별 판정·같은 선박 비교를 화물마다 동시에 돌린다(12번 차례 → 2번).
  · 혼재 서술 LLM(체크리스트)을 종합 문장 LLM 과 함께 기다린다(차례로 2번 → 동시에).
로컬 DB·Neo4j 를 쓴다(conftest). 화물 2종 이상인 접안 선박이 없으면 건너뛴다.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.agents.orchestrator.schemas import LLMSummary, OrchestratorRequest
from app.agents.orchestrator.service import orchestrate
from app.agents.safety.schemas import CargoRef, LLMAssessment
from app.agents.scheduling.schemas import VesselSpec
from app.llm.null_client import NullLLMClient

DELAY = 0.5

_Q_TARGET = text("""
    SELECT vc.callsgn, fa.wharf_name, array_agg(DISTINCT cm.chem_id) AS chems
    FROM mart.vessel_presence p
    JOIN mart.vessel_current_call vc ON vc.callsgn = upper(btrim(p.callsgn))
    JOIN mart.cargo_msds cm ON cm.port_call_key = vc.port_call_key AND cm.chem_id IS NOT NULL
    JOIN mart.facility_alias fa ON fa.source_name = p.berth_name AND fa.facility_type = 'BERTH'
    WHERE p.presence_zone = 'BERTH'
    GROUP BY 1, 2 HAVING count(DISTINCT cm.chem_id) >= 2
    ORDER BY 1 LIMIT 1
""")


class _SlowLLM:
    """호출마다 DELAY 만큼 기다리고, 동시에 몇 개가 떠 있었는지 센다."""

    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def generate_structured(self, *, system_prompt, user_prompt, schema):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(DELAY)
        finally:
            self.active -= 1
        if schema is LLMAssessment:
            empty = await NullLLMClient().generate_structured(system_prompt="", user_prompt="", schema=schema)
            return empty.model_copy(update={"checklist": ["시험용 확인 항목"]})
        if schema is LLMSummary:
            return LLMSummary(summary="시험용 종합 문장입니다.")
        raise AssertionError(schema)


async def _request(db):
    row = (await db.execute(_Q_TARGET)).first()
    if row is None:
        pytest.skip("화물 2종 이상인 접안 선박이 로컬 DB 에 없다")
    cs, wharf, chems = row
    chems = sorted(chems)
    now = datetime.now(timezone.utc)
    return OrchestratorRequest(
        vessel=VesselSpec(draught_m=6.0, dwt_t=None, name_hint=cs, call_sign=cs),
        cargo=CargoRef(chem_id=chems[0]), cargos=[CargoRef(chem_id=c) for c in chems[1:]],
        window_start=now, window_end=now + timedelta(hours=8),
        assigned_wharf_name=wharf, target_source="AIS",
    )


@pytest.mark.asyncio
async def test_safety_narration_and_summary_llm_overlap(db, neo4j):
    request = await _request(db)
    llm = _SlowLLM()
    result = await orchestrate(db, neo4j, llm, request)
    if result.safety_assessment is None:
        pytest.skip("선석·기상 단계에서 끝나 혼재 심사까지 가지 않았다")
    # 두 LLM 이 동시에 떠 있었다
    assert llm.max_active == 2
    # 나중에 채운 체크리스트가 결과에 실렸다(결과가 혼재 판정 객체를 복사하지 않는다)
    assert "시험용 확인 항목" in result.safety_assessment.checklist
    assert result.summary == "시험용 종합 문장입니다."

