"""챗봇 물질명 해석 회귀 테스트 (2026-09-27).

물질명 해석이 틀리면 다른 물질의 MSDS 로 답한다. 두 방향을 함께 지킨다.
  · 등재 물질 — 국문명·CAS·별칭 사전이 전부 제 물질로 해석된다(결정적 매칭, API 호출 없음).
  · 미등재 물질 — 비슷한 이름의 등재 물질로 해석되지 않는다. 결정적 매칭에 없으면 벡터로
    넘어가므로 임베딩 API 를 부른다 → RUN_LLM_EVAL=1 일 때만 돈다.

미등재 목록은 2026-09-27 msds_chemical 에 CAS·이름 모두 없음을 확인한 것이다.
evals/run_eval_v2.py 의 none 계층과 같은 목록이다.
"""
import os

import pytest
from sqlalchemy import text

from app.agents.chatbot.aliases import _RAW_ALIASES
from app.agents.chatbot.retrieval import resolve_chemical_names
from app.llm.factory import get_embedding_client


async def _resolve(db, name: str):
    matches, unresolved, _ = await resolve_chemical_names(db, get_embedding_client(), [name])
    return matches[0] if matches else None


async def _resolve_many(db, targets: dict[str, str]) -> dict[str, object]:
    """{질의: 기대 키} 를 한 번에 해석한다(이름 색인을 한 번만 읽는다).

    resolve_chemical_names 는 같은 물질로 해석된 질의를 하나로 합치므로, 기대 물질이 겹치지
    않게 여러 묶음으로 나눠 부른다. 돌려주는 값은 {질의: ChemicalMatch | None}.
    """
    rounds: list[dict[str, str]] = []
    for q, key in targets.items():
        for r in rounds:
            if key not in r.values():
                r[q] = key
                break
        else:
            rounds.append({q: key})
    found: dict[str, object] = {}
    for r in rounds:
        matches, _, _ = await resolve_chemical_names(db, get_embedding_client(), list(r))
        by_query = {m.query_name: m for m in matches}
        found.update({q: by_query.get(q) for q in r})
    return found


# ── 등재 물질: 결정적 매칭 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_registered_names_and_cas_resolve_to_themselves(db):
    rows = (await db.execute(text("SELECT chem_id, name_ko, cas_no FROM msds_chemical"))).all()
    assert rows, "msds_chemical 이 비어 있다"
    wrong = []
    for field in (1, 2):  # 국문명, CAS
        targets = {row[field]: row[0] for row in rows if row[field]}
        for q, m in (await _resolve_many(db, targets)).items():
            if m is None or m.chem_id != targets[q]:
                wrong.append((q, targets[q], m.chem_id if m else None))
    assert not wrong, f"제 물질로 해석되지 않은 이름·CAS: {wrong[:10]}"


@pytest.mark.asyncio
async def test_aliases_resolve_to_their_cas(db):
    registered = {cas for (cas,) in (await db.execute(text("SELECT cas_no FROM msds_chemical"))).all()}
    targets = {alias: cas for alias, cas in _RAW_ALIASES.items() if cas in registered}
    wrong = [(q, targets[q], m.cas_no if m else None)
             for q, m in (await _resolve_many(db, targets)).items()
             if m is None or m.cas_no != targets[q]]
    assert not wrong, f"별칭 해석 오류: {wrong[:10]}"


# ── 미등재 물질: 지어내지 않는다(벡터 경로, 임베딩 API 호출) ─────────────────

needs_api = pytest.mark.skipif(
    os.environ.get("RUN_LLM_EVAL") != "1", reason="임베딩 API 호출 — RUN_LLM_EVAL=1 일 때만",
)

UNREGISTERED = [
    "질산암모늄", "과산화수소", "시안화수소", "포스겐", "염소", "불화수소", "브롬", "히드라진",
    "니트로글리세린", "이황화탄소", "산화에틸렌", "아세트알데히드", "포름알데히드",
    "황화수소", "과염소산", "피크르산", "사염화탄소",
]


@needs_api
@pytest.mark.asyncio
@pytest.mark.parametrize("name", UNREGISTERED)
async def test_unregistered_is_not_resolved(db, name):
    m = await _resolve(db, name)
    assert m is None, f"미등재 '{name}' 이 '{m.name_ko}'({m.cas_no}, {m.method.value}) 로 해석됐다"


@needs_api
@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason=(
    "알려진 결함(2026-09-27): 벡터 유사도 0.7104 가 임계 0.70 을 넘어 '수소'(1333-74-0)로 해석된다. "
    "고치면 이 xfail 이 XPASS 로 실패하므로 표시를 지운다."
))
async def test_hydrogen_chloride_is_not_hydrogen(db):
    m = await _resolve(db, "염화수소")
    assert m is None, f"'염화수소' 가 '{m.name_ko}'({m.cas_no}) 로 해석됐다"
