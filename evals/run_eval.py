"""MSDS 챗봇 검색 평가셋 실행기.

**단위 테스트가 아니다.** 라이브 PostgreSQL·Neo4j와 OPENAI_API_KEY가 있어야 돌고
문항마다 임베딩 API를 부르므로 CI에 넣을 수 없다. 그래서 tests/가 아니라 evals/에 둔다.
청킹 규칙·임베딩 모델·프롬프트를 바꿨을 때 회귀를 확인하는 수동 측정 도구다.

실행:
    cd backend
    .venv/Scripts/python -m evals.run_eval              # 현재 적재된 인덱스로 평가
    .venv/Scripts/python -m evals.run_eval --verbose    # 실패 문항 상세 출력

계층별로 다른 것을 검증한다:
  chunk  — search_context()의 top_k 안에 정답 섹션이 들어오는가 (1위 / top6)
  column — msds_chemical 정형 컬럼에 값이 있는가 (벡터 검색과 무관)
  graph  — Neo4j 프로필에 해당 사실이 있는가
  none   — 미등재 물질이 unresolved로 떨어지는가 (지어내지 않는가)

DB와 임베딩 인덱스가 적재된 상태여야 한다.
"""
import argparse
import asyncio
import io
import json
import os
import sys
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.agents.chatbot import graph_queries
from app.agents.chatbot.retrieval import resolve_chemical_names, search_context
from app.config import get_settings
from app.database import AsyncSessionFactory
from app.llm.factory import get_embedding_client
from app.models import MsdsChemical
from app.neo4j_client import neo4j_client
from sqlalchemy import select

EVALSET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "msds_chatbot_evalset.json")
TOP_K = 6

# column 계층 문항이 요구하는 컬럼 — 질문 키워드로 고른다.
_COLUMN_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("인화점",), "flash_point_text"),
    (("끓는점",), "boiling_point_text"),
    (("증기압",), "vapor_pressure_text"),
    (("비중",), "specific_gravity_text"),
    (("노출기준", "TWA", "ppm"), "exposure_limit_kr"),
    (("용기등급", "포장등급"), "packing_group"),
    (("EMS",), "ems_fire"),
    (("신호어",), "signal_word"),
]

# graph 계층 문항이 요구하는 프로필 필드
_GRAPH_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("UN번호",), "un_no"),
    (("IMDG",), "imdg_classes"),
    (("혼재", "같이 두면"), "incompatible_categories"),
    (("GHS", "유해성 분류"), "hazard_classes"),
]


def _pick(question: str, hints) -> str | None:
    for keys, field in hints:
        if any(k in question for k in keys):
            return field
    return None


async def main(verbose: bool) -> None:
    data = json.load(open(EVALSET, encoding="utf-8"))
    cases = data["cases"]
    client = get_embedding_client()
    driver = neo4j_client.driver

    stat = defaultdict(lambda: {"n": 0, "pass": 0, "first": 0})
    failures: list[str] = []

    async with AsyncSessionFactory() as db:
        for case in cases:
            layer = case["expect_layer"]
            name, q = case["chemical"], case["question"]
            s = stat[layer]
            s["n"] += 1

            matches, unresolved = await resolve_chemical_names(db, client, [name])

            # ── none: 미등재 물질은 해석되지 않아야 한다 ─────────────────────
            if layer == "none":
                if not matches and unresolved:
                    s["pass"] += 1
                else:
                    failures.append(f"[{case['id']}] {name} — 미등재여야 하는데 "
                                    f"{matches[0].name_ko if matches else '?'}로 해석됨")
                continue

            if not matches:
                failures.append(f"[{case['id']}] {name} — 물질명 해석 실패")
                continue
            chem_id = matches[0].chem_id

            # ── column: 정형 컬럼에 값이 있는가 ──────────────────────────────
            if layer == "column":
                field = _pick(q, _COLUMN_HINTS)
                row = await db.scalar(select(MsdsChemical).where(MsdsChemical.chem_id == chem_id))
                value = getattr(row, field, None) if (row and field) else None
                if value:
                    s["pass"] += 1
                    s["first"] += 1
                else:
                    failures.append(f"[{case['id']}] {name} '{q}' — 컬럼 {field} 값 없음")
                continue

            # ── graph: 프로필에 해당 사실이 있는가 ───────────────────────────
            if layer == "graph":
                field = _pick(q, _GRAPH_HINTS)
                profiles = await graph_queries.fetch_profiles(driver, [chem_id])
                node = profiles.get(chem_id)
                value = node.get(field) if (node and field) else None
                if value:
                    s["pass"] += 1
                    s["first"] += 1
                else:
                    failures.append(f"[{case['id']}] {name} '{q}' — 그래프 {field} 없음")
                continue

            # ── chunk: 정답 섹션이 top_k 안에 오는가 ─────────────────────────
            expect = case["expect_section"]
            chunks = await search_context(db, client, f"{name} {q}", top_k=TOP_K, chem_ids=[chem_id])
            keys = [c.section_key for c in chunks]
            if keys and keys[0] == expect:
                s["first"] += 1
            if expect in keys:
                s["pass"] += 1
            else:
                rank = "top6 밖"
                failures.append(f"[{case['id']}] {name} '{q}' — 정답 {expect} {rank}, "
                                f"반환 {keys}")

    settings = get_settings()
    print(f"평가셋: {data['name']} v{data['version']}   문항 {len(cases)}개")
    print(f"임베딩 모델: {settings.embedding_model}\n")
    print(f"{'계층':<10}{'문항':>6}{'정답 근거 확보':>16}{'정답 섹션 1위':>16}")
    print("-" * 50)
    order = ["chunk", "column", "graph", "none"]
    tot_n = tot_p = tot_f = 0
    for layer in order:
        if layer not in stat:
            continue
        s = stat[layer]
        tot_n += s["n"]; tot_p += s["pass"]; tot_f += s["first"]
        first = f"{s['first']}/{s['n']}" if layer == "chunk" else "—"
        print(f"{layer:<10}{s['n']:>6}{s['pass']:>13}/{s['n']}{first:>16}")
    print("-" * 50)
    print(f"{'합계':<10}{tot_n:>6}{tot_p:>13}/{tot_n}   ({tot_p/tot_n*100:.1f}%)")

    if failures:
        print(f"\n실패 {len(failures)}건")
        for f in failures if verbose else failures[:12]:
            print(f"  {f}")
        if not verbose and len(failures) > 12:
            print(f"  … 외 {len(failures)-12}건 (--verbose로 전체 출력)")

    await neo4j_client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="MSDS 챗봇 검색 평가셋 실행")
    p.add_argument("--verbose", action="store_true", help="실패 문항 전체 출력")
    asyncio.run(main(p.parse_args().verbose))
