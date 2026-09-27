"""MSDS 챗봇 검색 평가셋 v2 — 등재 151종 전수 (2026-09-27).

v1(msds_chatbot_evalset.json, 80문항)은 2026-08-03 등재 36종으로 만든 것이라 지금의 151종을
대부분 덮지 못한다. v2 는 문항을 **DB 에서 결정적으로 생성**한다 — 정답이 원문에 실제로 있는
경우에만 문항을 만든다(v1 README 의 "원문에 답이 있는지 먼저 확인" 원칙을 코드로).

계층
  · chunk    물질 × 그 물질에 청크가 있는 섹션 × 섹션별 일반 질문 2개 → 정답 섹션이 top6 안인가
  · resolve  물질명(국문명) · CAS · 별칭 사전 전 항목 → 기대한 물질로 해석되는가
  · none     등재되지 않은 물질(CAS 로 부재 확인) → 해석되지 않아야 한다(지어내지 않음)

한계: 질문은 섹션별 일반 문형이라 v1(페르소나 작성)보다 쉽다. 플래너(LLM)는 건너뛴다(v1 과 같음).

실행: cd backend && .venv/Scripts/python -m evals.run_eval_v2 [--limit N]
"""
import argparse
import asyncio
import hashlib
import io
import json
import os
import sys
import time
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from sqlalchemy import text

from app.agents.chatbot.aliases import _RAW_ALIASES
from app.agents.chatbot.retrieval import resolve_chemical_names, search_context
from app.config import get_settings
from app.database import AsyncSessionFactory
from app.llm.factory import get_embedding_client

TOP_K = 6
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "eval_v2_last.json")

# 섹션별 일반 문형 — v1 문항 중 특정 수치·조건을 묻지 않는 것 위주(원문에 없을 수 있는 질문 배제).
TEMPLATES = {
    "detail02": ["어떤 유해성 분류를 받았어?", "경고표지에 뭐라고 적혀 있어야 해?"],
    "detail04": ["작업자가 흡입했을 때 응급조치 알려줘", "눈에 들어갔을 때 어떻게 해야 해?"],
    "detail05": ["화재 났을 때 무슨 소화제 써야 해?", "불나면 어떻게 대응해야 하나?"],
    "detail06": ["하역 중에 새면 어떻게 대처해?", "누출 시 환경 보호를 위한 조치가 뭐야?"],
    "detail07": ["저장할 때 주의사항 알려줘", "취급 시 주의사항 알려줘"],
    "detail08": ["취급할 때 어떤 보호구 착용해야 해?", "호흡기 보호구 뭐 써야 하지?"],
    "detail09": ["성상이랑 냄새는 어때?", "끓는점이나 녹는점 같은 물리적 성질 알려줘"],
    "detail10": ["화학적으로 안정한 물질인가?", "피해야 할 조건이 뭐가 있어?"],
    "detail11": ["흡입하면 얼마나 위험해? 급성독성 수치 알려줘", "인체에 어떤 독성이 있어?"],
    "detail12": ["바다에 유출되면 어류에 어떤 영향이 있어?", "수생환경 유해성 수치 알려줘"],
    "detail13": ["폐기할 때 주의사항 알려줘", "잔재물 폐기는 어떻게 해?"],
    "detail15": ["산업안전보건법상 어떤 규제를 받아?", "국내 법적 규제 현황 알려줘"],
}

# 미등재 물질 — 2026-09-27 msds_chemical 에 CAS·이름 모두 없음을 확인한 것만
UNREGISTERED = ["질산암모늄", "과산화수소", "시안화수소", "포스겐", "염소", "불화수소", "브롬", "히드라진",
                "니트로글리세린", "이황화탄소", "산화에틸렌", "아세트알데히드", "염화수소", "포름알데히드",
                "황화수소", "과염소산", "피크르산", "사염화탄소"]


async def build(db) -> list[dict]:
    chems = (await db.execute(text("SELECT chem_id, name_ko, cas_no FROM msds_chemical ORDER BY chem_id"))).all()
    sections = defaultdict(set)
    for chem_id, sec in (await db.execute(text("SELECT DISTINCT chem_id, section_key FROM msds_embedding"))).all():
        sections[chem_id].add(sec)
    cases = []
    for chem_id, name, cas in chems:
        for sec in sorted(sections[chem_id]):
            for q in TEMPLATES.get(sec, []):
                cases.append({"layer": "chunk", "chem_id": chem_id, "chemical": name, "question": q, "expect_section": sec})
        cases.append({"layer": "resolve", "chem_id": chem_id, "chemical": name, "via": "국문명"})
        if cas:
            cases.append({"layer": "resolve", "chem_id": chem_id, "chemical": cas, "via": "CAS"})
    by_cas = {cas: cid for cid, _, cas in chems if cas}
    for alias, cas in _RAW_ALIASES.items():
        if cas in by_cas:
            cases.append({"layer": "resolve", "chem_id": by_cas[cas], "chemical": alias, "via": "별칭"})
    for n in UNREGISTERED:
        cases.append({"layer": "none", "chem_id": None, "chemical": n})
    return cases


async def main(limit: int | None) -> None:
    client = get_embedding_client()
    t0 = time.perf_counter()
    async with AsyncSessionFactory() as db:
        cases = await build(db)
        if limit:
            cases = cases[:limit]
        stat = defaultdict(lambda: {"n": 0, "pass": 0, "first": 0})
        by_sec = defaultdict(lambda: {"n": 0, "pass": 0, "first": 0})
        failures = []
        for c in cases:
            s = stat[c["layer"] if c["layer"] != "resolve" else f"resolve/{c['via']}"]
            s["n"] += 1
            if c["layer"] in ("resolve", "none"):
                matches, unresolved, _ = await resolve_chemical_names(db, client, [c["chemical"]])
                got = matches[0].chem_id if matches else None
                ok = (got is None) if c["layer"] == "none" else (got == c["chem_id"])
                s["pass"] += ok
                if not ok:
                    failures.append({**c, "got": got, "got_name": matches[0].name_ko if matches else None})
                continue
            chunks = await search_context(db, client, f"{c['chemical']} {c['question']}", top_k=TOP_K, chem_ids=[c["chem_id"]])
            keys = [k.section_key for k in chunks]
            b = by_sec[c["expect_section"]]
            b["n"] += 1
            if keys and keys[0] == c["expect_section"]:
                s["first"] += 1; b["first"] += 1
            if c["expect_section"] in keys:
                s["pass"] += 1; b["pass"] += 1
            else:
                failures.append({**c, "returned": keys})

    elapsed = time.perf_counter() - t0
    settings = get_settings()
    print(f"평가셋 v2 (DB 생성) 문항 {len(cases)}개 · 임베딩 {settings.embedding_model} · {elapsed:.0f}초\n")
    tot_n = tot_p = 0
    for k in sorted(stat):
        s = stat[k]; tot_n += s["n"]; tot_p += s["pass"]
        first = f"  1위 {s['first']}/{s['n']} ({s['first']/s['n']:.1%})" if k == "chunk" else ""
        print(f"{k:<14}{s['n']:>6}  확보 {s['pass']}/{s['n']} ({s['pass']/s['n']:.1%}){first}")
    print(f"{'합계':<14}{tot_n:>6}  {tot_p}/{tot_n} ({tot_p/tot_n:.1%})\n")
    print("섹션별(chunk):")
    for k in sorted(by_sec):
        b = by_sec[k]
        print(f"  {k}  {b['n']:>4}  확보 {b['pass']/b['n']:.1%}  1위 {b['first']/b['n']:.1%}")
    print(f"\n실패 {len(failures)}건 (상세: {OUT})")
    for f in failures[:40]:
        print("  ", json.dumps(f, ensure_ascii=False))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"stat": stat, "by_section": by_sec, "failures": failures, "n": len(cases), "sec": round(elapsed)},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int)
    asyncio.run(main(p.parse_args().limit))
