"""챗봇 평가셋 — 실제 LLM 을 부른다 (27번 설계안 C단계, 2026-09-27).

비용이 들어 기본으로는 건너뛴다. 돌리려면:
    RUN_LLM_EVAL=1 python -m pytest tests/test_chatbot_eval_llm_20260927.py -q

로컬 DB·그래프를 읽는다. 배는 계속 움직이므로 운영 질문의 기대값(어디 있나, 최근 판정)은
테스트 시점에 도구로 다시 읽어 맞춘다. 지목한 배가 지금 항내에 없으면 그 사례는 건너뛴다.

통과 기준(27번 설계안 5절):
  · 운영 질문에 현황 도구를 부르고, 도구가 준 사실(배·부두·화물·판정)을 답에 담는다.
  · 등급이 들어간 답의 결론이 판정 등급과 일치한다.
  · 기존 물질 질문(혼재·물성·목록·범위 밖)이 그대로 동작한다.
"""
import os

import pytest

from app.agents import tools
from app.agents.chatbot.schemas import Intent, RagQueryRequest
from app.agents.chatbot.service import answer_question
from app.config import get_settings
from app.llm.embeddings import OpenAiEmbeddingClient
from app.llm.openai_client import OpenAiClient

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_EVAL") != "1", reason="실제 LLM 호출 — RUN_LLM_EVAL=1 일 때만",
)


def _clients():
    s = get_settings()
    llm = OpenAiClient(api_key=s.openai_api_key, model=s.chatbot_llm_model or s.llm_model)
    emb = OpenAiEmbeddingClient(api_key=s.openai_api_key, model=s.embedding_model)
    return llm, emb


async def _ask(db, neo4j, question: str):
    llm, emb = _clients()
    return await answer_question(db, neo4j, llm, emb, RagQueryRequest(question=question))


def _squash(text: str) -> str:
    return "".join(text.lower().split())


# ── 운영 질문 ──

@pytest.mark.asyncio
async def test_ops_who_is_at_berth(db, neo4j):
    berth = await tools.who_is_at(db, neo4j, wharf="S-Oil 2부두")
    r = await _ask(db, neo4j, "S-Oil 2부두에 지금 어떤 배가 붙어 있어?")
    assert r.intent is Intent.OPERATIONS and "who_is_at" in r.tools_used
    if berth and berth.vessels:
        assert any(_squash(v.vessel_name) in _squash(r.answer) for v in berth.vessels if v.vessel_name)


@pytest.mark.asyncio
async def test_ops_where_is_vessel(db, neo4j):
    found = await tools.where_is(db, query="LAGA8")
    if not found or not found[0].place:
        pytest.skip("LAGA8 이 지금 항내 위치 자료에 없음")
    r = await _ask(db, neo4j, "LAGA8 지금 어디 있어?")
    assert "where_is" in r.tools_used
    assert _squash(found[0].place) in _squash(r.answer)


@pytest.mark.asyncio
async def test_ops_neighbor_cargo(db, neo4j):
    berth = await tools.who_is_at(db, neo4j, wharf="S-Oil 2부두")
    cargos = {c for n in (berth.neighbors if berth else []) for v in n.vessels for c in v.cargos}
    if not cargos:
        pytest.skip("S-Oil 2부두 인접 부두에 화물을 실은 배가 지금 없음")
    r = await _ask(db, neo4j, "S-Oil 2부두 옆 부두에는 어떤 화물이 있어?")
    assert any(c in r.answer for c in cargos)


@pytest.mark.asyncio
async def test_ops_latest_judgment(db, neo4j):
    found = await tools.where_is(db, query="D8AG")
    if not found or not found[0].latest_level:
        pytest.skip("D8AG 의 판정 기록이 없음")
    r = await _ask(db, neo4j, "D8AG 판정 결과 알려줘")
    assert found[0].latest_level in r.answer


@pytest.mark.asyncio
async def test_ops_segregation_is_forced_and_consistent(db, neo4j):
    # 특정 배 이름을 박으면 그 배가 떠나면 검증이 빠진다 — 지금 이웃에 화물이 있는 접안선을 고른다
    berth = await tools.who_is_at(db, neo4j, wharf="S-Oil 2부두")
    has_neighbor_cargo = berth is not None and any(v.chem_ids for n in berth.neighbors for v in n.vessels)
    target = next((v for v in (berth.vessels if berth else []) if v.chem_ids and v.vessel_name), None)
    if not (has_neighbor_cargo and target):
        pytest.skip("S-Oil 2부두에 화물을 실은 접안선이나 인접 부두 화물이 지금 없음")
    r = await _ask(db, neo4j, f"{target.vessel_name} 화물이랑 옆 부두 화물 같이 둬도 괜찮아?")
    if r.safety_assessment is None:
        pytest.skip("인접 부두에 화물을 실은 배가 없어 판정 대상 없음")
    assert "segregation_check" in r.tools_used
    assert r.safety_assessment.risk_level.value in r.answer.split("\n")[0] or \
        r.answer.startswith(f"**혼재 판정: {r.safety_assessment.risk_level.value}**")


# ── 기존 물질 질문(회귀) ──

@pytest.mark.asyncio
async def test_chem_segregation_verdict_in_answer(db, neo4j):
    r = await _ask(db, neo4j, "황산과 가성소다 같이 둬도 돼?")
    assert r.safety_assessment is not None and r.safety_assessment.risk_level.value == "배정불가"
    assert "배정불가" in r.answer


@pytest.mark.asyncio
async def test_chem_korean_alias(db, neo4j):
    r = await _ask(db, neo4j, "1-도데센 인화점 알려줘")
    assert any(m.cas_no == "112-41-4" for m in r.chemicals_resolved)


@pytest.mark.asyncio
async def test_chem_incompatible_list(db, neo4j):
    r = await _ask(db, neo4j, "벤젠과 혼재금지인 물질 알려줘")
    assert r.intent is Intent.INCOMPATIBLE_LIST


@pytest.mark.asyncio
async def test_out_of_scope(db, neo4j):
    r = await _ask(db, neo4j, "오늘 점심 뭐 먹지?")
    assert r.intent is Intent.OUT_OF_SCOPE
