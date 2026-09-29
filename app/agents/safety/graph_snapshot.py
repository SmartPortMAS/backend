"""혼재 판정 기준 그래프의 메모리 사본 (2026-09-29).

판정(_compute_verdict)이 Neo4j 에서 읽는 것은 화물 대 화물 **기준 데이터**뿐이다 — MSDS 혼재금지
카테고리, IMDG Class·격리표, 46 CFR 150 호환성 그룹·예외. 로더를 다시 돌리지 않는 한 바뀌지 않는다.
그런데 판정 한 번에 이 조회가 화물 × 9번 나갔고, 운영 Neo4j(Aura)는 조회 한 번이 약 300ms 라
화물 6종 배의 규칙 판정이 12초 넘게 걸렸다(9/29 실측). 그래서 통째로 한 번 읽어 두고, 같은 질문에
graph_queries 의 Cypher 와 **같은 답**을 메모리에서 낸다(tests/test_graph_snapshot_20260929.py 가 대조).

사본은 REFRESH_SECONDS 마다 다시 읽는다 — 로더를 돌리면 늦어도 그만큼 뒤에 반영된다.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from neo4j import AsyncDriver

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 600

_QUERIES = {
    "chemicals": "MATCH (c:Chemical) RETURN c.id AS c, c.name_ko AS name",
    "avoids": """MATCH (c:Chemical)-[:INCOMPATIBLE_WITH]->(m)
                 RETURN c.id AS c, elementId(m) AS m, m.name AS name, 'IncompatibleMaterial' IN labels(m) AS im""",
    "classified": """MATCH (c:Chemical)-[:IS_CLASSIFIED_AS]->(m)
                     RETURN c.id AS c, elementId(m) AS m, m.name AS name, 'IncompatibleMaterial' IN labels(m) AS im""",
    "imdg": "MATCH (c:Chemical)-[:HAS_IMDG_CLASS]->(k:ImdgClass) RETURN c.id AS c, elementId(k) AS k, k.code AS code",
    "segregate": """MATCH (a:ImdgClass)-[s:SEGREGATE]->(b:ImdgClass)
                    RETURN elementId(a) AS a, elementId(b) AS b, s.code AS code""",
    "no_seg": """MATCH (a:ImdgClass)-[:NO_SEGREGATION_REQUIRED]->(b:ImdgClass)
                 RETURN elementId(a) AS a, elementId(b) AS b""",
    "groups": """MATCH (c:Chemical)-[:IN_COMPATIBILITY_GROUP]->(g:CompatibilityGroup)
                 RETURN c.id AS c, elementId(g) AS g, g.group_no AS no, g.group_type AS type, g.name AS name""",
    "group_incompat": """MATCH (a:CompatibilityGroup)-[:INCOMPATIBLE_WITH_GROUP]->(b:CompatibilityGroup)
                         RETURN elementId(a) AS a, elementId(b) AS b""",
    "safe_exc": "MATCH (a:Chemical)-[:BULK_COMPAT_SAFE_EXCEPTION]->(b:Chemical) RETURN a.id AS a, b.id AS b",
    "blocked_exc": "MATCH (a:Chemical)-[:BULK_COMPAT_BLOCKED_EXCEPTION]->(b:Chemical) RETURN a.id AS a, b.id AS b",
}


@dataclass
class GraphSnapshot:
    names: dict[str, str | None]                                  # Chemical id -> name_ko
    avoids: dict[str, list[tuple[str, str, bool]]]                # id -> [(카테고리 노드, 이름, IncompatibleMaterial?)]
    classified: dict[str, list[tuple[str, str, bool]]]
    imdg: dict[str, list[tuple[str, str]]]                        # id -> [(Class 노드, code)]
    segregate: dict[tuple[str, str], list[str]]                   # (Class 노드, Class 노드) -> [격리 code]
    no_seg: set[tuple[str, str]]
    groups: dict[str, list[tuple[str, int, str, str]]]            # id -> [(그룹 노드, 번호, 구분, 이름)]
    group_incompat: dict[tuple[str, str], int]                    # (그룹 노드, 그룹 노드) -> 관계 수
    safe_exc: set[tuple[str, str]]
    blocked_exc: set[tuple[str, str]]
    live_categories: set[str]

    # ── graph_queries 의 같은 이름 함수와 같은 답 ────────────────────────────────

    def incompatible_conflicts(self, target: str, adjacent: list[str]) -> list[dict]:
        if not adjacent or target not in self.names:
            return []
        rows: set[tuple] = set()
        target_cls = {m for m, _, _ in self.classified.get(target, [])}
        for b in adjacent:
            if b not in self.names:
                continue
            b_cls = {m for m, _, _ in self.classified.get(b, [])}
            for m, name, _ in self.avoids.get(target, []):
                if m in b_cls:
                    rows.add((b, self.names[b], name, "target_incompatible_with_adjacent"))
            for m, name, _ in self.avoids.get(b, []):
                if m in target_cls:
                    rows.add((b, self.names[b], name, "adjacent_incompatible_with_target"))
        return [dict(chem_id=r[0], name_ko=r[1], category=r[2], direction=r[3]) for r in rows]

    def imdg_segregation_conflicts(self, target: str, adjacent: list[str]) -> list[dict]:
        if not adjacent:
            return []
        return [
            dict(chem_id=b, name_ko=self.names[b], target_class=ca_code, adjacent_class=cb_code, segregation_code=code)
            for ca, ca_code in self.imdg.get(target, [])
            for b in adjacent
            for cb, cb_code in self.imdg.get(b, [])
            for code in self.segregate.get((ca, cb), [])
        ]

    def imdg_no_segregation_required(self, target: str, adjacent: list[str]) -> set[str]:
        return {
            b
            for ca, _ in self.imdg.get(target, [])
            for b in adjacent
            for cb, _ in self.imdg.get(b, [])
            if (ca, cb) in self.no_seg
        }

    def imdg_classes(self, chem_ids: list[str]) -> dict[str, str]:
        return {c: code for c in dict.fromkeys(chem_ids) for _, code in self.imdg.get(c, [])}

    def bulk_group_conflicts(self, target: str, adjacent: list[str]) -> list[dict]:
        if not adjacent:
            return []
        return [
            dict(chem_id=b, name_ko=self.names[b])
            for ga, *_ in self.groups.get(target, [])
            for b in adjacent
            for gb, *_ in self.groups.get(b, [])
            for _ in range(self.group_incompat.get((ga, gb), 0))
        ]

    def bulk_groups(self, chem_ids: list[str]) -> dict[str, tuple[int, str, str]]:
        return {c: (no, type_, name) for c in dict.fromkeys(chem_ids) for _, no, type_, name in self.groups.get(c, [])}

    def bulk_exceptions(self, target: str, adjacent: list[str]) -> tuple[set[str], set[str]]:
        return (
            {b for b in adjacent if (target, b) in self.safe_exc},
            {b for b in adjacent if (target, b) in self.blocked_exc},
        )

    def assessability_facts(self, chem_ids: list[str]) -> dict[str, tuple[set[str], set[str]]]:
        return {
            c: (
                {name for _, name, im in self.avoids.get(c, []) if im and name},
                {name for _, name, im in self.classified.get(c, []) if im and name},
            )
            for c in chem_ids if c in self.names
        }


async def load_snapshot(driver: AsyncDriver) -> GraphSnapshot:
    async def run(query: str) -> list[dict]:
        async with driver.session() as session:
            async def _tx(tx):
                return [record.data() async for record in await tx.run(query)]
            return await session.execute_read(_tx)

    keys = list(_QUERIES)
    raw = dict(zip(keys, await asyncio.gather(*(run(_QUERIES[k]) for k in keys))))

    def by_chem(rows, *cols):
        out = defaultdict(list)
        for r in rows:
            out[r["c"]].append(tuple(r[c] for c in cols))
        return dict(out)

    segregate = defaultdict(list)
    for r in raw["segregate"]:
        segregate[(r["a"], r["b"])].append(r["code"])
    group_incompat = defaultdict(int)
    for r in raw["group_incompat"]:
        group_incompat[(r["a"], r["b"])] += 1
    classified = by_chem(raw["classified"], "m", "name", "im")
    return GraphSnapshot(
        names={r["c"]: r["name"] for r in raw["chemicals"]},
        avoids=by_chem(raw["avoids"], "m", "name", "im"),
        classified=classified,
        imdg=by_chem(raw["imdg"], "k", "code"),
        segregate=dict(segregate),
        no_seg={(r["a"], r["b"]) for r in raw["no_seg"]},
        groups=by_chem(raw["groups"], "g", "no", "type", "name"),
        group_incompat=dict(group_incompat),
        safe_exc={(r["a"], r["b"]) for r in raw["safe_exc"]},
        blocked_exc={(r["a"], r["b"]) for r in raw["blocked_exc"]},
        # find_live_categories 와 같다 — Chemical 이 IS_CLASSIFIED_AS 로 가리키는 IncompatibleMaterial 이름
        live_categories={name for rows in classified.values() for _, name, im in rows if im and name},
    )


_snapshot: GraphSnapshot | None = None
_loaded_at = 0.0
_lock: asyncio.Lock | None = None


async def get_snapshot(driver: AsyncDriver) -> GraphSnapshot:
    """사본을 돌려준다. 없거나 REFRESH_SECONDS 가 지났으면 먼저 다시 읽는다(동시 요청은 한 번만 읽는다)."""
    global _snapshot, _loaded_at, _lock
    if _snapshot is not None and time.monotonic() - _loaded_at < REFRESH_SECONDS:
        return _snapshot
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _snapshot is None or time.monotonic() - _loaded_at >= REFRESH_SECONDS:
            started = time.monotonic()
            _snapshot = await load_snapshot(driver)
            _loaded_at = time.monotonic()
            logger.info("혼재 판정 그래프 사본 적재: 화학물질 %d종, %.1fs", len(_snapshot.names), _loaded_at - started)
    return _snapshot
