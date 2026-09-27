"""챗봇 LLM 평가 — 도구 호출·판정 일치를 지금 DB 로 생성한 문항으로 잰다 (2026-09-27).

tests/test_chatbot_eval_llm_20260927.py(9문항)는 회귀 확인용이라 보고서 근거로는 적다. 여기서는
지금 항내 상황(접안선·부두·판정 기록)과 합성 화물 물질쌍으로 문항을 만들어, 정답을 도구·규칙엔진이
직접 낸 값과 대조한다. 실제 LLM(CHATBOT_LLM_MODEL)을 부른다 — 비용이 든다.

유형
  where      "<호출부호> 지금 어디 있어?"          where_is 호출 + 답에 실제 접안 부두
  who        "<부두>에 지금 어떤 배가 붙어 있어?"   who_is_at 호출 + 답에 접안선 1척 이상
  judgment   "<호출부호> 판정 결과 알려줘"          답에 최신 판정 등급
  segregation "<A>와 <B> 같이 둬도 돼?"             챗봇 등급 = 규칙엔진 하한 + 답에 그 등급 단어
  chem_info  "<물질> 인화점 알려줘"                  기대 물질로 해석 + 물질 정보 의도
  out_of_scope 무관한 질문                          범위 밖 의도

실행: cd backend && .venv/Scripts/python -m evals.run_chatbot_llm_eval
"""
import asyncio
import io
import itertools
import json
import os
import random
import sys
import time
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from sqlalchemy import text

from app.agents import tools
from app.agents.chatbot.schemas import Intent, RagQueryRequest
from app.agents.chatbot.service import answer_question
from app.agents.safety.schemas import AdjacentCargo, CargoRef, SafetyAssessmentRequest
from app.agents.safety.service import assess_verdict
from app.config import get_settings
from app.database import AsyncSessionFactory
from app.llm.embeddings import OpenAiEmbeddingClient
from app.llm.openai_client import OpenAiClient
from app.neo4j_client import neo4j_client

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "chatbot_llm_eval_last.json")
OFF_TOPIC = ["오늘 점심 뭐 먹지?", "주식 추천해줘", "서울 날씨 어때?", "파이썬 리스트 정렬하는 법 알려줘",
             "영화 추천 좀 해줘", "축구 경기 결과 알려줘"]


def squash(s: str | None) -> str:
    return "".join((s or "").lower().split())


def place_in(place: str, answer: str) -> tuple[bool, bool]:
    """(엄격: 부두명 전체가 그대로, 느슨: 공백으로 나뉜 조각이 모두 들어 있음)."""
    a = squash(answer)
    return squash(place) in a, all(squash(t) in a for t in place.split())


async def build(db, driver) -> list[dict]:
    rng = random.Random(20260927)
    cases = []
    rows = (await db.execute(text("""
        SELECT DISTINCT ON (callsgn) callsgn, vessel_name, berth_name FROM mart.vessel_presence
        WHERE presence_zone = 'BERTH' AND callsgn IS NOT NULL ORDER BY callsgn, received_at_utc DESC"""))).all()
    berthed = [r for r in rows if r.berth_name]
    for r in rng.sample(berthed, min(25, len(berthed))):
        cases.append({"type": "where", "q": f"{r.callsgn} 지금 어디 있어?", "cs": r.callsgn})
    wharves = sorted({r.berth_name for r in berthed})
    for w in rng.sample(wharves, min(20, len(wharves))):
        cases.append({"type": "who", "q": f"{w}에 지금 어떤 배가 붙어 있어?", "wharf": w})
    judged = []
    for r in berthed:
        found = await tools.where_is(db, query=r.callsgn)
        if found and found[0].latest_level:
            judged.append(r.callsgn)
    for cs in rng.sample(judged, min(15, len(judged))):
        cases.append({"type": "judgment", "q": f"{cs} 판정 결과 알려줘", "cs": cs})

    chems = (await db.execute(text("""
        SELECT DISTINCT m.chem_id, m.name_ko FROM upa_cargo_manifest u JOIN msds_chemical m USING (chem_id)"""))).all()
    by_level = defaultdict(list)
    for (a, an), (b, bn) in itertools.combinations(chems, 2):
        levels = []
        for x, y in ((a, b), (b, a)):
            v = await assess_verdict(db, driver, SafetyAssessmentRequest(
                target_cargo=CargoRef(chem_id=x),
                adjacent_cargos=[AdjacentCargo(berth_name="평가", cargo=CargoRef(chem_id=y), distance_m=None)]))
            levels.append(v.rule_engine_floor.value)
        by_level[levels[0]].append((an, bn, sorted(set(levels))))
    for level, n in (("배정불가", 12), ("위험", 10), ("안전", 10)):
        for an, bn, lv in rng.sample(by_level[level], min(n, len(by_level[level]))):
            cases.append({"type": "segregation", "q": f"{an}와 {bn} 같이 둬도 돼?", "expect": lv})

    info = (await db.execute(text("""
        SELECT chem_id, name_ko FROM msds_chemical WHERE flash_point_text IS NOT NULL
          AND name_ko !~ '^[A-Za-z0-9 ,()-]+$' ORDER BY chem_id"""))).all()
    for chem_id, name in rng.sample(info, min(12, len(info))):
        cases.append({"type": "chem_info", "q": f"{name} 인화점 알려줘", "chem_id": chem_id})
    for q in OFF_TOPIC:
        cases.append({"type": "out_of_scope", "q": q})
    return cases


async def judge(db, driver, llm, emb, c) -> dict:
    t = time.perf_counter()
    r = await answer_question(db, driver, llm, emb, RagQueryRequest(question=c["q"]))
    res = {**c, "sec": round(time.perf_counter() - t, 1), "intent": r.intent.value,
           "tools": r.tools_used, "answer": r.answer[:300]}
    typ = c["type"]
    if typ == "where":
        found = await tools.where_is(db, query=c["cs"])
        place = found[0].place if found else None
        strict, loose = place_in(place, r.answer) if place else (False, False)
        res.update(tool_ok="where_is" in r.tools_used, fact=place, strict=strict, ok=("where_is" in r.tools_used and loose))
    elif typ == "who":
        b = await tools.who_is_at(db, driver, wharf=c["wharf"])
        names = [v.vessel_name or v.call_sign for v in (b.vessels if b else []) if (v.vessel_name or v.call_sign)]
        hit = any(squash(n) in squash(r.answer) for n in names)
        res.update(tool_ok="who_is_at" in r.tools_used, fact=names, ok=("who_is_at" in r.tools_used and hit))
    elif typ == "judgment":
        found = await tools.where_is(db, query=c["cs"])
        lv = found[0].latest_level if found else None
        res.update(tool_ok="where_is" in r.tools_used, fact=lv, ok=bool(lv and lv in r.answer))
    elif typ == "segregation":
        got = r.safety_assessment.risk_level.value if r.safety_assessment else None
        res.update(fact=c["expect"], got=got, ok=(got in c["expect"] and got in r.answer))
    elif typ == "chem_info":
        res.update(ok=any(m.chem_id == c["chem_id"] for m in r.chemicals_resolved) and r.intent is Intent.CHEMICAL_INFO)
    else:
        res.update(ok=r.intent is Intent.OUT_OF_SCOPE)
    return res


async def main():
    s = get_settings()
    model = s.chatbot_llm_model or s.llm_model
    llm = OpenAiClient(api_key=s.openai_api_key, model=model)
    emb = OpenAiEmbeddingClient(api_key=s.openai_api_key, model=s.embedding_model)
    driver = neo4j_client.driver
    results = []
    async with AsyncSessionFactory() as db:
        cases = await build(db, driver)
        print(f"문항 {len(cases)}개 · 모델 {model}", flush=True)
        for i, c in enumerate(cases, 1):
            try:
                results.append(await judge(db, driver, llm, emb, c))
            except Exception as e:  # 한 문항 실패가 전체를 멈추지 않게 — 실패로 센다
                results.append({**c, "ok": False, "error": repr(e)[:200]})
            if i % 10 == 0:
                print(f"  {i}/{len(cases)}", flush=True)
    stat = defaultdict(lambda: {"n": 0, "ok": 0, "tool_ok": 0, "strict": 0, "sec": 0.0})
    for r in results:
        st = stat[r["type"]]; st["n"] += 1; st["ok"] += bool(r.get("ok")); st["sec"] += r.get("sec", 0)
        st["tool_ok"] += bool(r.get("tool_ok")); st["strict"] += bool(r.get("strict"))
    tot = sum(v["n"] for v in stat.values()); ok = sum(v["ok"] for v in stat.values())
    print(f"\n{'유형':<14}{'문항':>5}{'통과':>8}{'평균초':>8}")
    for k, v in stat.items():
        extra = f"  도구 {v['tool_ok']}/{v['n']}" if k in ("where", "who", "judgment") else ""
        extra += f"  엄격표기 {v['strict']}/{v['n']}" if k == "where" else ""
        print(f"{k:<14}{v['n']:>5}{v['ok']:>5}/{v['n']:<3}{v['sec']/v['n']:>7.1f}{extra}")
    print(f"{'합계':<14}{tot:>5}{ok:>5}/{tot}  ({ok/tot:.1%})")
    for r in results:
        if not r.get("ok"):
            print("  실패:", json.dumps({k: r.get(k) for k in ("type", "q", "fact", "got", "intent", "tools", "answer", "error")}, ensure_ascii=False))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"model": model, "stat": stat, "results": results}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    await neo4j_client.close()


if __name__ == "__main__":
    asyncio.run(main())
