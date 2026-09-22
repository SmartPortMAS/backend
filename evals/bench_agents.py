"""에이전트별 응답시간·GraphRAG 추론 성능 측정기.

**단위 테스트가 아니다.** run_eval.py와 마찬가지로 라이브 PostgreSQL·Neo4j와
OPENAI_API_KEY가 있어야 돌고, LLM을 쓰는 경로(안전관제 서술·챗봇)는 호출마다
과금된다. CI에 넣지 말 것.

측정 대상:
  1) 에이전트별 end-to-end 응답시간 (기상·스케줄링·안전관제·GraphRAG 챗봇)
  2) GraphRAG 파이프라인 단계별 소요시간 분해 (물질명 해석 / 그래프 N-홉 / 벡터 검색)
  3) LLM 경로와 결정론 경로의 분리 측정

FastAPI 요청 1건과 같은 조건을 만들기 위해 **반복마다 새 AsyncSession**을 연다
(세션을 재사용하면 SQLAlchemy identity map 때문에 2회차부터 DB 왕복이 사라져
실제 응답시간보다 빠르게 나온다).

실행:
    cd backend
    .venv/Scripts/python -m evals.bench_agents               # 전체
    .venv/Scripts/python -m evals.bench_agents --no-llm      # LLM 경로 제외(무과금)
    .venv/Scripts/python -m evals.bench_agents --repeat 20
"""
import argparse
import asyncio
import io
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.agents.chatbot import graph_queries as chat_graph
from app.agents.chatbot.retrieval import resolve_chemical_names, search_context
from app.agents.chatbot.schemas import RagQueryRequest
from app.agents.chatbot.service import answer_question
from app.agents.safety.schemas import AdjacentCargo, CargoRef, SafetyAssessmentRequest
from app.agents.safety.service import assess_safety, assess_verdict
from app.agents.scheduling.schemas import SchedulingRequest, VesselSpec
from app.agents.scheduling.service import find_berth_candidates
from app.agents.weather.schemas import WeatherAssessmentRequest
from app.agents.weather.service import assess_weather
from app.config import get_settings
from app.database import AsyncSessionFactory
from app.llm.factory import get_embedding_client, get_llm_client
from app.models import CHUNK_KIND_SECTION, MsdsChemical, MsdsEmbedding
from app.neo4j_client import neo4j_client
from sqlalchemy import select

# 관측 최신치가 실린 시각으로 고정한다. 지금 시각으로 재면 관측이 3시간을 넘겨
# '판단불가' 조기 반환 경로만 타서, 임계값 비교·예보 조회를 포함한 실제 판정
# 경로의 응답시간을 못 잰다.
AS_OF = datetime(2026, 8, 23, 14, 30, tzinfo=timezone.utc)


async def _pgvector_search(db, vector, *, chem_ids, top_k):
    """search_context()의 SQL만 떼어낸 것 — 임베딩 API 왕복을 뺀 순수 DB 검색시간용."""
    distance = MsdsEmbedding.embedding.cosine_distance(vector)
    stmt = (
        select(MsdsEmbedding.chem_id, MsdsEmbedding.section_key,
               (1 - distance).label("score"))
        .where(MsdsEmbedding.chunk_kind == CHUNK_KIND_SECTION)
        .where(MsdsEmbedding.chem_id.in_(chem_ids))
        .order_by(distance)
        .limit(top_k)
    )
    return (await db.execute(stmt)).all()


def _stat(samples):
    s = sorted(samples)
    return {
        "n": len(s),
        "mean": statistics.mean(s),
        "p50": statistics.median(s),
        "p95": s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))],
        "min": s[0],
        "max": s[-1],
    }


async def _measure(label: str, fn, repeat: int, results: dict):
    """fn()을 repeat회 실행하고 ms 통계를 담는다. 첫 1회는 워밍업으로 버린다."""
    last = await fn()  # 워밍업 (커넥션 풀·쿼리 플랜 캐시)
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        last = await fn()
        samples.append((time.perf_counter() - t0) * 1000)
    results[label] = _stat(samples)
    st = results[label]
    print(f"  {label:<46} p50 {st['p50']:8.1f}ms  p95 {st['p95']:8.1f}ms  "
          f"mean {st['mean']:8.1f}ms  (n={st['n']})")
    return last


# ─────────────────────────────────────────────────────────────────────────────
# 1) 에이전트별 응답시간
# ─────────────────────────────────────────────────────────────────────────────

async def bench_weather(repeat: int, results: dict) -> None:
    print("\n[기상분석 에이전트] 결정론 규칙엔진 (LLM 미사용)")

    async def run_basic():
        async with AsyncSessionFactory() as db:
            return await assess_weather(db, WeatherAssessmentRequest(
                berth_group="정일1/2부두(산암리)", as_of=AS_OF))

    async def run_forecast():
        async with AsyncSessionFactory() as db:
            return await assess_weather(db, WeatherAssessmentRequest(
                berth_group="정일1/2부두(산암리)", as_of=AS_OF,
                expected_completion_at=AS_OF + timedelta(hours=12)))

    r = await _measure("현재 관측 기반 판정", run_basic, repeat, results)
    print(f"      -> status={r.status.value}")
    r = await _measure("현재 판정 + 단기예보 사전경고", run_forecast, repeat, results)
    print(f"      -> status={r.status.value} forecast_warning={r.forecast_warning is not None}")


async def bench_scheduling(repeat: int, results: dict):
    print("\n[스케줄링 에이전트] 결정론 다단계 게이트 (LLM 미사용)")
    req = SchedulingRequest(
        vessel=VesselSpec(draught_m=9.5, dwt_t=20000, name_hint="BENCH-TEST"),
        cargo=CargoRef(chem_id="001008", name_hint="벤젠"),
        window_start=AS_OF,
        window_end=AS_OF + timedelta(hours=18),
    )

    async def run():
        async with AsyncSessionFactory() as db:
            return await find_berth_candidates(db, neo4j_client.driver, req)

    r = await _measure("선석후보 top-3 탐색(게이트+정렬+폴백)", run, repeat, results)
    print(f"      -> 후보 {len(r.candidates)}개 / 적격 총 {r.total_eligible_count}개")
    for c in r.candidates:
        print(f"         {c.rank}. {c.berth_id} {c.wharf_name} "
              f"({c.occupancy_status.value}, 인접화물 {len(c.adjacent_cargos)}종)")
    return r


async def bench_safety(repeat: int, results: dict, sched_result, use_llm: bool) -> None:
    print("\n[안전관제 에이전트] 규칙엔진 4축 하한 + LLM 서술")

    # 스케줄링 결과의 실제 인접 화물을 그대로 넘긴다(설계상 그대로 연결되는 경로).
    adjacent = []
    if sched_result and sched_result.candidates:
        adjacent = list(sched_result.candidates[0].adjacent_cargos)
    if not adjacent:
        # 후보에 인접 화물이 없으면 검증 사례(황산 반응성 충돌)로 대체한다.
        adjacent = [AdjacentCargo(berth_name="BENCH-인접선석",
                                  cargo=CargoRef(chem_id="001049", name_hint="황산"))]
    print(f"      인접 화물 {len(adjacent)}종으로 측정")

    req = SafetyAssessmentRequest(
        target_cargo=CargoRef(chem_id="001008", name_hint="벤젠", unload_method_name="펌프"),
        adjacent_cargos=adjacent,
    )

    async def run_verdict():
        async with AsyncSessionFactory() as db:
            return await assess_verdict(db, neo4j_client.driver, req)

    r = await _measure("판정만(규칙엔진 4축, LLM 미호출)", run_verdict, repeat, results)
    print(f"      -> risk_level={r.risk_level.value} 혼재충돌 {len(r.conflicts)}건 "
          f"IMDG {len(r.imdg_conflicts)}건 벌크 {len(r.bulk_compatibility_conflicts)}건")

    if not use_llm:
        return

    llm = get_llm_client()

    async def run_full():
        async with AsyncSessionFactory() as db:
            return await assess_safety(db, neo4j_client.driver, llm, req)

    r = await _measure("판정 + LLM 체크리스트·설명 생성", run_full, max(3, repeat // 3), results)
    print(f"      -> risk_level={r.risk_level.value} (rule_floor={r.rule_engine_floor.value}) "
          f"체크리스트 {len(r.checklist)}항목")


# ─────────────────────────────────────────────────────────────────────────────
# 2) GraphRAG 챗봇 — end-to-end + 단계별 분해
# ─────────────────────────────────────────────────────────────────────────────

CHAT_CASES = [
    ("chemical_info", "벤젠 취급 시 착용해야 할 보호구가 뭐야?"),
    ("incompatibility_check", "벤젠과 황산을 인접 선석에 둬도 되나요?"),
    ("incompatible_list", "메틸 알코올과 같이 두면 안 되는 화물 알려줘"),
    ("safety_general", "액체화물 하역 중 누출이 나면 어떻게 대응해야 하나요?"),
]


async def bench_chatbot(repeat: int, results: dict) -> None:
    print("\n[GraphRAG 챗봇] LLM 2회 + 그래프/벡터/정형 3계층 검색")
    llm, emb = get_llm_client(), get_embedding_client()
    n = max(3, repeat // 3)

    for intent, question in CHAT_CASES:
        async def run(q=question):
            async with AsyncSessionFactory() as db:
                return await answer_question(
                    db, neo4j_client.driver, llm, emb, RagQueryRequest(question=q))

        r = await _measure(f"end-to-end [{intent}]", run, n, results)
        graph_n = (len(r.graph_evidence.profiles) + len(r.graph_evidence.incompatible_groups)
                   if r.graph_evidence else 0)
        print(f"      -> intent={r.intent.value} confidence={r.confidence.value} "
              f"벡터근거 {len(r.retrieved_chunks)}건 / 그래프근거 {graph_n}건 / "
              f"출처 {len(r.sources)}건 / 답변 {len(r.answer)}자")


async def bench_chatbot_stages(repeat: int, results: dict) -> None:
    """GraphRAG 파이프라인 단계별 소요시간 분해."""
    print("\n[GraphRAG 단계별 분해] 벤젠-황산 혼재 질의 기준")
    emb = get_embedding_client()

    async def stage_resolve():
        async with AsyncSessionFactory() as db:
            return await resolve_chemical_names(db, emb, ["벤젠", "황산"])

    async def stage_vector():
        async with AsyncSessionFactory() as db:
            return await search_context(db, emb, "벤젠 취급 시 보호구", top_k=6, chem_ids=["001008"])

    async def stage_graph_profile():
        return await chat_graph.fetch_profiles(neo4j_client.driver, ["001008", "001049"])

    async def stage_graph_2hop():
        return await chat_graph.fetch_incompatible_groups(neo4j_client.driver, "001008")

    async def stage_graph_imdg():
        return await chat_graph.fetch_imdg_segregation_groups(neo4j_client.driver, "001008")

    async def stage_graph_bulk():
        return await chat_graph.fetch_bulk_compatibility_groups(neo4j_client.driver, "001008")

    # 벡터 검색 총시간에는 질의 임베딩 API 왕복(네트워크)이 포함된다. 그래프
    # 조회(수십 ms)와 같은 축에서 비교하려면 이 둘을 분리해야 한다.
    async def stage_embed_api():
        return await emb.embed_one("벤젠 취급 시 보호구")

    question_vector = await emb.embed_one("벤젠 취급 시 보호구")

    async def stage_pgvector_only():
        async with AsyncSessionFactory() as db:
            return await _pgvector_search(db, question_vector, chem_ids=["001008"], top_k=6)

    await _measure("물질명 해석 4단계(결정적 매칭 적중)", stage_resolve, repeat, results)
    r = await _measure("벡터 검색 총시간(임베딩 API + pgvector)", stage_vector, repeat, results)
    print(f"      -> 청크 {len(r)}건, 최고점수 {r[0].score if r else '-'}")
    await _measure("  ├ 질의 임베딩 API 왕복(OpenAI)", stage_embed_api, max(3, repeat // 3), results)
    r = await _measure("  └ pgvector HNSW 검색만(순수 DB)", stage_pgvector_only, repeat, results)
    print(f"      -> 청크 {len(r)}건")
    await _measure("그래프 프로필 조회(1-홉, 2종)", stage_graph_profile, repeat, results)
    r = await _measure("혼재금지 2-홉 양방향 탐색", stage_graph_2hop, repeat, results)
    print(f"      -> 카테고리 {len(r)}개")
    r = await _measure("IMDG 격리표 3-홉 탐색", stage_graph_imdg, repeat, results)
    print(f"      -> Class 조합 {len(r)}개")
    r = await _measure("벌크 호환성그룹 3-홉 탐색", stage_graph_bulk, repeat, results)
    print(f"      -> 그룹 조합 {len(r)}개")


async def bench_vector_only(repeat: int, results: dict) -> None:
    """물질명 해석에서 결정적 매칭이 실패해 벡터 경로까지 가는 최악 경로."""
    print("\n[GraphRAG 최악 경로] 결정적 매칭 실패 -> 임베딩 API 호출 포함")
    emb = get_embedding_client()

    async def run():
        async with AsyncSessionFactory() as db:
            return await resolve_chemical_names(db, emb, ["밴젠"])  # 오타 -> 벡터 경로

    r = await _measure("물질명 해석(벡터 폴백, 임베딩 API 왕복)", run, max(3, repeat // 3), results)
    print(f"      -> 해석 {len(r[0])}건 / 미해석 {len(r[1])}건 / 되묻기후보 {r[2]}")


# ─────────────────────────────────────────────────────────────────────────────
# 3) GraphRAG 추론 정확성 검증 — 시간이 아니라 "맞게 추론했는가"를 본다
# ─────────────────────────────────────────────────────────────────────────────

# (설명, 대상 chem_id, 인접 chem_id, 기대 판정 축, 기대 등급)
VERIFY_PAIRS = [
    ("MSDS 반응성 혼재금지 탐지", "001049", "황산", "001067", "아세톤", "msds"),
    ("IMDG 공인 격리표 탐지", "000557", "수소", "031533", "가솔린(GASOLINE)", "imdg"),
    ("벌크 호환성그룹 탐지", "000034", "에탄올", "001049", "황산", "bulk"),
    ("벌크 예외 — 강제차단", "001143", "아크릴로니트릴", "017853", "폴리에테르 폴리올 수지", "bulk"),
    ("벌크 예외 — 안전확인", "001143", "아크릴로니트릴", "000218", "트리에탄올아민", "none"),
    ("무관 조합(오탐 확인)", "001008", "벤젠", "001032", "톨루엔", "none"),
]

# (설명, 입력, 기대 해석 결과 chem_id 또는 None=미해석이어야 함)
VERIFY_NAMES = [
    ("1단계 CAS 번호", "71-43-2", "001008"),
    ("2단계 정확 명칭", "황산", "001049"),
    ("3단계 별칭 사전", "휘발유", None),          # 실제 매핑 대상은 실행 결과로 확인
    ("3단계 별칭 사전(영문 약칭)", "IPA", None),
    ("4단계 벡터 — 오타", "밴젠", "UNRESOLVED"),
    ("4단계 벡터 — 미등재 물질 거부", "염산", "UNRESOLVED"),
    ("4단계 벡터 — 무의미 입력 거부", "zzzzq", "UNRESOLVED"),
]


async def verify_graph_reasoning() -> None:
    print("\n" + "=" * 92)
    print("[GraphRAG 추론 정확성 검증] 규칙엔진 4축 — 실제 그래프·DB로 직접 판정")
    print("=" * 92)
    print(f"{'검증 항목':<34}{'대상':<10}{'인접':<22}{'판정':<8}"
          f"{'MSDS':>5}{'IMDG':>5}{'벌크':>5}{'미평가':>7}")
    print("-" * 92)
    for label, t_id, t_name, a_id, a_name, _axis in VERIFY_PAIRS:
        req = SafetyAssessmentRequest(
            target_cargo=CargoRef(chem_id=t_id, name_hint=t_name),
            adjacent_cargos=[AdjacentCargo(berth_name="검증-인접선석",
                                           cargo=CargoRef(chem_id=a_id, name_hint=a_name))],
        )
        async with AsyncSessionFactory() as db:
            v = await assess_verdict(db, neo4j_client.driver, req)
        print(f"{label:<34}{t_name[:8]:<10}{a_name[:20]:<22}{v.risk_level.value:<8}"
              f"{len(v.conflicts):>5}{len(v.imdg_conflicts):>5}"
              f"{len(v.bulk_compatibility_conflicts):>5}{len(v.unassessed_pairs):>7}")
        for c in v.conflicts:
            print(f"      · MSDS 반응성 충돌: {c.adjacent_name} — 카테고리 '{c.shared_category}' ({c.direction})")
        for c in v.imdg_conflicts:
            print(f"      · IMDG 격리: Class {c.target_imdg_class}↔{c.adjacent_imdg_class} "
                  f"격리코드 {c.segregation_code}")
        for c in v.bulk_compatibility_conflicts:
            print(f"      · 벌크 호환성: {c.reason}")


async def verify_name_resolution() -> None:
    print("\n" + "=" * 92)
    print("[GraphRAG 추론 정확성 검증] 물질명 4단계 해석 — 미등재를 '안전'으로 오인하지 않는가")
    print("=" * 92)
    emb = get_embedding_client()
    print(f"{'검증 항목':<34}{'입력':<12}{'해석 결과':<26}{'방식':<12}{'점수':>8}")
    print("-" * 92)
    for label, query, _expect in VERIFY_NAMES:
        async with AsyncSessionFactory() as db:
            matches, unresolved, near = await resolve_chemical_names(db, emb, [query])
        if matches:
            m = matches[0]
            print(f"{label:<34}{query:<12}{(m.name_ko or m.chem_id)[:24]:<26}"
                  f"{m.method.value:<12}{(m.score if m.score is not None else 1.0):>8.4f}")
        else:
            hint = f" -> 되묻기 '{near[query][0]}'({near[query][1]})" if query in near else " (되묻기 없음)"
            print(f"{label:<34}{query:<12}{'미해석(거부)' + hint:<26}{'-':<12}{'-':>8}")


async def verify_llm_floor(repeat: int) -> None:
    """LLM이 규칙엔진 하한을 뒤집지 못하는지 — 등급 결정 권한이 규칙엔진에 있는가."""
    print("\n" + "=" * 92)
    print("[GraphRAG 추론 정확성 검증] LLM 하한 강제 — 규칙엔진 등급과 최종 등급 일치 여부")
    print("=" * 92)
    llm = get_llm_client()
    agree = 0
    total = 0
    for label, t_id, t_name, a_id, a_name, _axis in VERIFY_PAIRS:
        req = SafetyAssessmentRequest(
            target_cargo=CargoRef(chem_id=t_id, name_hint=t_name),
            adjacent_cargos=[AdjacentCargo(berth_name="검증-인접선석",
                                           cargo=CargoRef(chem_id=a_id, name_hint=a_name))],
        )
        async with AsyncSessionFactory() as db:
            r = await assess_safety(db, neo4j_client.driver, llm, req)
        total += 1
        ok = r.risk_level == r.rule_engine_floor
        agree += int(ok)
        print(f"  {label:<34} 최종 {r.risk_level.value:<6} / 규칙하한 {r.rule_engine_floor.value:<6} "
              f"{'일치' if ok else '불일치'}  체크리스트 {len(r.checklist)}항목")
    print(f"\n  -> 규칙엔진 하한 유지율 {agree}/{total} ({agree / total * 100:.0f}%)")


async def verify_full_matrix() -> None:
    """등재 화물 전 조합(순서쌍) 전수 추론 — 커버리지·등급분포·처리량.

    LLM을 부르지 않는 규칙엔진 경로만 돈다(무과금). 그래프 탐색이 실제로 몇 건의
    조합에서 근거를 찾아내는지, 근거가 없어 '판정불가'로 남는 조합이 몇 %인지를
    본다 — "모르면 안전이라고 답하지 않는다"가 데이터상 몇 건에 해당하는지의 실측.
    """
    print("\n" + "=" * 92)
    print("[GraphRAG 추론 정확성 검증] 등재 화물 전 조합 전수 판정 (규칙엔진 4축, LLM 미사용)")
    print("=" * 92)

    async with AsyncSessionFactory() as db:
        rows = list(await db.scalars(
            select(MsdsChemical).where(MsdsChemical.quality_flag == "OK")))
    ids = [r.chem_id for r in rows]
    print(f"  대상 화물 {len(ids)}종 → 순서쌍 {len(ids) * (len(ids) - 1)}건 전수 판정")

    levels: dict[str, int] = {}
    axes = {"msds": 0, "imdg": 0, "bulk": 0, "packing": 0}
    unassessed_pairs = 0
    assessability: dict[str, int] = {}
    t0 = time.perf_counter()
    for t_id in ids:
        for a_id in ids:
            if t_id == a_id:
                continue
            req = SafetyAssessmentRequest(
                target_cargo=CargoRef(chem_id=t_id),
                adjacent_cargos=[AdjacentCargo(berth_name="M", cargo=CargoRef(chem_id=a_id))],
            )
            async with AsyncSessionFactory() as db:
                v = await assess_verdict(db, neo4j_client.driver, req)
            levels[v.risk_level.value] = levels.get(v.risk_level.value, 0) + 1
            axes["msds"] += bool(v.conflicts)
            axes["imdg"] += bool(v.imdg_conflicts)
            axes["bulk"] += bool(v.bulk_compatibility_conflicts)
            axes["packing"] += bool(v.packaging_violations)
            unassessed_pairs += bool(v.unassessed_pairs)
            for p in v.unassessed_pairs:
                assessability[p.assessability] = assessability.get(p.assessability, 0) + 1
    total = sum(levels.values())
    elapsed = time.perf_counter() - t0

    print(f"\n  판정 등급 분포 (총 {total}건)")
    for level in ("안전", "주의", "위험", "배정불가"):
        n = levels.get(level, 0)
        print(f"    {level:<8}{n:>6}건 ({n / total * 100:>5.1f}%)")
    print(f"\n  축별 탐지 건수(중복 포함)")
    for k, label in (("msds", "MSDS 반응성 혼재금지"), ("imdg", "IMDG 공인 격리표"),
                     ("bulk", "벌크 호환성그룹"), ("packing", "포장·하역방식")):
        print(f"    {label:<24}{axes[k]:>6}건 ({axes[k] / total * 100:>5.1f}%)")
    print(f"    {'근거 불완전(판정불가 표기)':<24}{unassessed_pairs:>6}건 "
          f"({unassessed_pairs / total * 100:>5.1f}%)")
    for label, n in sorted(assessability.items(), key=lambda kv: -kv[1]):
        print(f"      · {label:<20}{n:>6}건 ({n / total * 100:>5.1f}%)")
    print(f"\n  전수 소요 {elapsed:.1f}s — 조합당 평균 {elapsed / total * 1000:.1f}ms "
          f"(순차 실행 기준 초당 {total / elapsed:.1f}건)")


async def main(repeat: int, use_llm: bool, matrix: bool) -> None:
    settings = get_settings()
    print("=" * 92)
    print("SmartPort MAS — 에이전트별 응답시간 / GraphRAG 추론 성능 측정")
    print(f"측정 시각: {datetime.now(timezone.utc).isoformat(timespec='seconds')}  "
          f"반복 {repeat}회(워밍업 1회 별도)")
    print(f"LLM: {settings.llm_provider}/{settings.llm_model}   임베딩: {settings.embedding_model}")
    print(f"LLM 경로 포함: {use_llm}")
    print("=" * 92)

    results = {}
    await bench_weather(repeat, results)
    sched = await bench_scheduling(repeat, results)
    await bench_safety(repeat, results, sched, use_llm)
    await bench_chatbot_stages(repeat, results)
    await bench_vector_only(repeat, results)
    if use_llm:
        await bench_chatbot(repeat, results)

    print("\n" + "=" * 92)
    print(f"{'측정 항목':<48}{'p50(ms)':>10}{'p95(ms)':>10}{'평균(ms)':>10}{'n':>5}")
    print("-" * 92)
    for label, st in results.items():
        print(f"{label:<48}{st['p50']:>10.1f}{st['p95']:>10.1f}{st['mean']:>10.1f}{st['n']:>5}")
    print("=" * 92)

    await verify_graph_reasoning()
    await verify_name_resolution()
    if matrix:
        await verify_full_matrix()
    if use_llm:
        await verify_llm_floor(repeat)

    await neo4j_client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="에이전트 응답시간 / GraphRAG 성능 측정")
    p.add_argument("--repeat", type=int, default=10, help="반복 횟수(기본 10)")
    p.add_argument("--no-llm", action="store_true", help="LLM 호출 경로 제외(무과금)")
    p.add_argument("--matrix", action="store_true", help="등재 화물 전 조합 전수 판정 포함")
    a = p.parse_args()
    asyncio.run(main(a.repeat, not a.no_llm, a.matrix))
