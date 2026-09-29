"""혼재 판정 그래프 메모리 사본이 Cypher 조회와 같은 답을 내는가 (2026-09-29).

판정은 graph_queries 의 Cypher 9개 대신 graph_snapshot 사본을 쓴다. 로컬 Neo4j 의 모든 Chemical 을
대상으로, 이웃 화물 묶음을 바꿔 가며 두 쪽의 답을 대조한다(행 순서는 Neo4j 도 정하지 않으므로 무시).
"""
import json
import random

import pytest

from app.agents.safety import graph_queries as gq
from app.agents.safety.graph_snapshot import load_snapshot


def _rows(rows):
    return sorted(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows)


@pytest.mark.asyncio
async def test_snapshot_matches_cypher_for_every_chemical(neo4j):
    snap = await load_snapshot(neo4j)
    ids = sorted(snap.names)
    assert len(ids) > 50, "로컬 그래프에 화학물질이 거의 없다"
    rng = random.Random(20260929)
    assert snap.live_categories == await gq.find_live_categories(neo4j)

    for target in ids:
        # 이웃 묶음: 무작위 8종 + 자기 자신 + 그래프에 없는 id(PostgreSQL 에만 있는 화물)
        adjacent = list(dict.fromkeys(rng.sample(ids, 8) + [target, "NOT_IN_GRAPH"]))
        all_ids = [target, *adjacent]
        kw = dict(target_chem_id=target, adjacent_chem_ids=adjacent)

        assert _rows(snap.incompatible_conflicts(target, adjacent)) == _rows(await gq.find_incompatible_conflicts(neo4j, **kw)), target
        assert _rows(snap.imdg_segregation_conflicts(target, adjacent)) == _rows(await gq.find_imdg_segregation_conflicts(neo4j, **kw)), target
        assert snap.imdg_no_segregation_required(target, adjacent) == await gq.find_imdg_no_segregation_required(neo4j, **kw), target
        assert snap.imdg_classes(all_ids) == await gq.find_imdg_classes(neo4j, chem_ids=all_ids), target
        assert _rows(snap.bulk_group_conflicts(target, adjacent)) == _rows(await gq.find_bulk_group_conflicts(neo4j, **kw)), target
        assert snap.bulk_groups(all_ids) == await gq.find_bulk_groups(neo4j, chem_ids=all_ids), target
        assert snap.bulk_exceptions(target, adjacent) == await gq.find_bulk_exceptions(neo4j, **kw), target
        assert snap.assessability_facts(all_ids) == await gq.find_assessability_facts(neo4j, chem_ids=all_ids), target


@pytest.mark.asyncio
async def test_snapshot_catches_known_conflicts(neo4j):
    """무작위 묶음이 충돌을 거의 안 뽑으면 위 대조가 빈 목록끼리만 비교한다 — 충돌이 실제로 있는 쌍도 대조한다."""
    snap = await load_snapshot(neo4j)
    pairs = [(a, b) for a in snap.groups for b in snap.groups if snap.bulk_group_conflicts(a, [b])][:40]
    pairs += [(a, b) for a in snap.avoids for b in snap.classified if snap.incompatible_conflicts(a, [b])][:40]
    assert len(pairs) >= 40, "충돌 쌍을 충분히 찾지 못했다"
    for a, b in pairs:
        kw = dict(target_chem_id=a, adjacent_chem_ids=[b])
        assert _rows(snap.bulk_group_conflicts(a, [b])) == _rows(await gq.find_bulk_group_conflicts(neo4j, **kw))
        assert _rows(snap.incompatible_conflicts(a, [b])) == _rows(await gq.find_incompatible_conflicts(neo4j, **kw))
