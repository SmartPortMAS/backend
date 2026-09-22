"""151종 전수 순서쌍 안전 판정 매트릭스 재계산 (LLM 미사용).

왜 필요한가
-----------
`app/agents/safety/` 의 핵심 주석에 적힌 실측 수치("36종 순서쌍 1,260개 전수",
"배정불가 62건", "IMDG 단독 등급상승 304건")는 전부 **Neo4j Chemical 36종**
시절 기준이다. 2026-09-16 에 다음이 바뀌면서 그 숫자들이 전부 무효가 됐다:

  · Neo4j Chemical            36 -> 151종
  · 벌크 호환성그룹 매핑       35 -> 117종 (46 CFR 150 Table 1 원문 기반)
  · 벌크 비호환 그룹쌍         21 -> 125쌍 (46 CFR 150 Figure 1 전체 판독)

이 스크립트는 그 수치를 현재 데이터로 다시 만든다. 판정 로직은 건드리지 않고
`safety.service._compute_verdict`(LLM 미사용 경로)를 그대로 호출하므로,
운영 판정과 측정값이 어긋날 수 없다.

실행:
    cd backend
    .venv/Scripts/python -m scripts.recompute_safety_matrix
    .venv/Scripts/python -m scripts.recompute_safety_matrix --limit 20   # 표본만
"""

import argparse
import asyncio
import io
import json
import sys
import time
from collections import Counter

from sqlalchemy import text

from app.agents.safety.schemas import AdjacentCargo, CargoRef, SafetyAssessmentRequest
from app.agents.safety.service import _compute_verdict
from app.database import AsyncSessionFactory
from app.neo4j_client import neo4j_client

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

CONCURRENCY = 8


async def _load_chem_ids() -> list[str]:
    async with AsyncSessionFactory() as db:
        rows = await db.execute(text("SELECT chem_id FROM msds_chemical ORDER BY chem_id"))
        return [r[0] for r in rows]


async def _one_pair(driver, target: str, adjacent: str, sem: asyncio.Semaphore) -> dict:
    """한 순서쌍의 판정. 실패해도 전체를 죽이지 않고 error 로 표시한다."""
    async with sem:
        try:
            async with AsyncSessionFactory() as db:
                v = await _compute_verdict(
                    db,
                    driver,
                    SafetyAssessmentRequest(
                        target_cargo=CargoRef(chem_id=target),
                        # berth_name/distance_m 은 IMDG 응답 표시용이라 등급 판정에는
                        # 쓰이지 않는다(service.py 상단 docstring). 필수 필드라 자리만
                        # 채운다 — 값이 판정을 바꾸지 않음을 전제로 한 측정이다.
                        adjacent_cargos=[
                            AdjacentCargo(cargo=CargoRef(chem_id=adjacent), berth_name="(matrix)")
                        ],
                    ),
                )
            return {
                "t": target,
                "a": adjacent,
                "level": v.rule_engine_floor.value,
                "msds": len(v.conflicts),
                "bulk": len(v.bulk_compatibility_conflicts),
                "pack": len(v.packaging_violations),
                "unassessed": len(v.unassessed_pairs),
                "imdg": len(v.imdg_conflicts),
            }
        except Exception as exc:  # noqa: BLE001
            return {"t": target, "a": adjacent, "level": "ERROR", "err": str(exc)[:120]}


async def main(limit: int | None) -> None:
    chem_ids = await _load_chem_ids()
    if limit:
        chem_ids = chem_ids[:limit]
    n = len(chem_ids)
    pairs = [(t, a) for t in chem_ids for a in chem_ids if t != a]
    print(f"화물 {n}종 / 순서쌍 {len(pairs):,}개 — 동시 {CONCURRENCY}")

    driver = neo4j_client.driver
    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[dict] = []
    t0 = time.time()

    # 진행률을 보이게 청크로 나눠 돌린다(전부 gather 하면 끝날 때까지 무소식).
    CHUNK = 2000
    for i in range(0, len(pairs), CHUNK):
        batch = pairs[i : i + CHUNK]
        results.extend(await asyncio.gather(*(_one_pair(driver, t, a, sem) for t, a in batch)))
        done = len(results)
        el = time.time() - t0
        print(f"  {done:,}/{len(pairs):,}  ({done/len(pairs)*100:4.1f}%)  {el:6.1f}s", flush=True)

    await neo4j_client.close()

    lv = Counter(r["level"] for r in results)
    print("\n=== 등급 분포 ===")
    for k, c in lv.most_common():
        print(f"  {k:8s} {c:7,}  {c/len(results)*100:5.2f}%")

    ok = [r for r in results if r["level"] != "ERROR"]
    print("\n=== 축별 관여 건수 (해당 축에서 충돌/근거가 잡힌 쌍) ===")
    for axis, label in [("bulk", "벌크 호환성그룹"), ("msds", "MSDS 텍스트 혼재금지"),
                        ("pack", "용기등급 대비 하역방식"), ("unassessed", "판정불가(근거없음)"),
                        ("imdg", "IMDG 격리(참고)")]:
        c = sum(1 for r in ok if r.get(axis, 0) > 0)
        print(f"  {label:24s} {c:7,}  {c/len(ok)*100:5.2f}%")

    blocked = [r for r in ok if r["level"] == "배정불가"]
    print(f"\n=== 배정불가 {len(blocked):,}건의 축 구성 ===")
    print(f"  벌크 충돌 있음 : {sum(1 for r in blocked if r['bulk'] > 0):,}")
    print(f"  MSDS 충돌 있음 : {sum(1 for r in blocked if r['msds'] > 0):,}")
    print(f"  용기등급 위반  : {sum(1 for r in blocked if r['pack'] > 0):,}")

    err = [r for r in results if r["level"] == "ERROR"]
    if err:
        print(f"\n오류 {len(err):,}건 — 예: {err[0].get('err')}")

    out = "scripts/safety_matrix_result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"n_chemicals": n, "n_pairs": len(results),
                   "levels": dict(lv), "results": results}, f, ensure_ascii=False)
    print(f"\n상세 저장: backend/{out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="화물 수 제한(표본 실행)")
    a = ap.parse_args()
    asyncio.run(main(a.limit))
