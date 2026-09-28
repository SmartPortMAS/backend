"""2026-09-28 운영 DB 커넥션 풀 고갈 회귀 테스트(docs/31).

/alerts 계산이 Neo4j 를 기다리는 동안 DB 트랜잭션을 연 채였고, TTL 캐시는 계산 중에
들어온 요청을 합치지 못해 요청마다 연결을 하나씩 쥐었다. 두 가지를 막는다.
"""

import asyncio

import pytest

from app.agents.safety.berth_alerts import build_berth_alerts
from app.api.v1 import dashboard


@pytest.mark.asyncio
async def test_build_berth_alerts_leaves_no_open_transaction(db, neo4j):
    # 트랜잭션이 열려 있으면 연결이 풀로 돌아가지 않는다
    await build_berth_alerts(db, neo4j)
    assert not db.in_transaction()


@pytest.mark.asyncio
async def test_cached_runs_producer_once_for_concurrent_requests(monkeypatch):
    monkeypatch.setattr(dashboard, "_TTL_CACHE", {})
    monkeypatch.setattr(dashboard, "_TTL_LOCKS", {})
    calls = 0

    async def producer():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return ["result"]

    results = await asyncio.gather(*(dashboard._cached("k", producer) for _ in range(5)))

    assert calls == 1
    assert results == [["result"]] * 5


@pytest.mark.asyncio
async def test_cached_retries_after_producer_failure(monkeypatch):
    # 실패는 캐시하지 않는다 — 기다리던 요청이 다음 계산을 한다
    monkeypatch.setattr(dashboard, "_TTL_CACHE", {})
    monkeypatch.setattr(dashboard, "_TTL_LOCKS", {})
    calls = 0

    async def producer():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return ["ok"]

    with pytest.raises(RuntimeError):
        await dashboard._cached("k", producer)
    assert await dashboard._cached("k", producer) == ["ok"]
