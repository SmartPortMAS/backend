"""
선석 대체가능성(SUBSTITUTABLE_WITH) 생성

핵심 통찰: 액체화물 부두는 육상 파이프라인이 특정 탱크단지로 고정 연결된 전용부두다.
따라서 물리 제원(DWT, 흘수)이 맞아도 다른 운영사 선석으로는 못 옮긴다. 대체는 사실상
같은 운영사(같은 탱크팜) 안에서만 가능하고, 그마저 화물종류와 선박 크기가 맞아야 한다.
없으면 정박지 대기가 현실이다.

이 관계는 ADJACENT_TO(물리적 인접 = 혼재/근접 위험)와 완전히 다르다.
  ADJACENT_TO      : 좌표 거리 기반. 옆 선석이라 혼재 위험이 있나?
  SUBSTITUTABLE_WITH: 운영사/파이프라인 기반. 이 선석 못 쓰면 저 선석으로 옮길 수 있나?

대체 경로는 두 단계로 본다.
  1) 시설 내부(internal): 같은 부두에 선석이 2개 이상이면 내부에서 옮김. 가장 안전한 대체.
  2) 시설 간(cross): 같은 운영사 + 화물종류 교집합. 같은 탱크팜의 다른 부두로 옮김.
  둘 다 없으면 anchorage_only = 정박지 대기 확정.

런타임 게이트(스케줄러): 대상 선박 product in shared_products AND ship.dwt <= to_max_dwt
AND ship.draft <= to_depth. 정적 엣지는 후보만 만들고, 실제 가부는 선박 제원으로 최종 판정.

주의: 파이프라인-탱크 세부 연결은 터미널 내부(비공개)다. '같은 운영사 + 화물종류 호환'을
공개데이터 기반 최선 근사로 쓰고, 정밀화는 가상 시뮬레이션(탱크/파이프라인)으로 보완한다.
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = next((d for d in [os.path.join(_HERE, "..", "data"), _HERE] if os.path.isdir(d)), _HERE)
_MASTER = os.path.join(_DATA, "onsan_berth_master.csv")
_OUT_EDGES = os.path.join(_DATA, "onsan_berth_substitutability.csv")
_OUT_SUMMARY = os.path.join(_DATA, "onsan_berth_substitution_summary.csv")

PRODUCTS = {
    "liquid_chem": {"케미칼"},
    "liquid_chem_oil": {"유류", "케미칼"},
    "petroleum": {"유류"},
    "crude_oil": {"원유"},
}


def _i(v):
    v = (v or "").strip()
    return int(float(v)) if v else None


def _f(v):
    v = (v or "").strip()
    return float(v) if v else None


def load_berths(path=_MASTER):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append({
                "berth_id": (r.get("berth_id") or "").strip(),
                "berth_name": (r.get("berth_name") or "").strip(),
                "operator": (r.get("operator") or "").strip(),
                "cargo_class": (r.get("cargo_class") or "").strip(),
                "max_dwt": _f(r.get("접안능력_DWT")),
                "depth": _f(r.get("수심_m")),
                "berth_count": _i(r.get("선석수")) or 1,
            })
    return rows


def build(berths):
    by_op = defaultdict(list)
    for b in berths:
        by_op[b["operator"]].append(b)

    edges = []
    for op, group in by_op.items():
        for a in group:
            for b in group:
                if a["berth_id"] == b["berth_id"]:
                    continue
                shared = PRODUCTS.get(a["cargo_class"], set()) & PRODUCTS.get(b["cargo_class"], set())
                if shared:
                    edges.append({
                        "from_berth": a["berth_id"],
                        "to_berth": b["berth_id"],
                        "operator": op,
                        "shared_products": "/".join(sorted(shared)),
                        "to_max_dwt": "" if b["max_dwt"] is None else int(b["max_dwt"]),
                        "to_depth_m": "" if b["depth"] is None else b["depth"],
                    })

    cross = defaultdict(list)
    for e in edges:
        cross[e["from_berth"]].append(e["to_berth"])

    summary = []
    for b in berths:
        internal = b["berth_count"] >= 2
        cross_subs = cross.get(b["berth_id"], [])
        anchorage_only = (not internal) and (len(cross_subs) == 0)
        summary.append({
            "berth_id": b["berth_id"],
            "berth_name": b["berth_name"],
            "operator": b["operator"],
            "선석수": b["berth_count"],
            "internal_substitution": "Y" if internal else "N",
            "cross_substitutes": "/".join(cross_subs) if cross_subs else "",
            "anchorage_only": "Y" if anchorage_only else "N",
        })
    return edges, summary


if __name__ == "__main__":
    berths = load_berths()
    edges, summary = build(berths)

    anchorage_only = [s for s in summary if s["anchorage_only"] == "Y"]
    print(f"선석시설 {len(berths)}개, 시설간 대체엣지 {len(edges)}개, 정박지대기확정(anchorage_only) {len(anchorage_only)}개\n")

    print("=== 실제 대체 풀 (연결성분) ===")
    parent = {b["berth_id"]: b["berth_id"] for b in berths}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for e in edges:
        parent[find(e["from_berth"])] = find(e["to_berth"])
    comp = defaultdict(list)
    for b in berths:
        comp[find(b["berth_id"])].append(b["berth_id"])
    for members in sorted(comp.values(), key=lambda m: -len(m)):
        if len(members) >= 2:
            print(f"  {sorted(members)}")
    print()

    print("=== berth별 대체 요약 ===")
    print(f"  {'berth':10s} {'선석':3s} {'내부':4s} {'시설간대체':22s} {'정박지대기'}")
    for s in summary:
        print(f"  {s['berth_id']:10s} {s['선석수']:<3} {s['internal_substitution']:4s} "
              f"{s['cross_substitutes'] or '-':22s} {'★' if s['anchorage_only']=='Y' else ''}")
    print()

    with open(_OUT_EDGES, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["from_berth", "to_berth", "operator", "shared_products", "to_max_dwt", "to_depth_m"])
        w.writeheader(); w.writerows(edges)
    with open(_OUT_SUMMARY, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["berth_id", "berth_name", "operator", "선석수", "internal_substitution", "cross_substitutes", "anchorage_only"])
        w.writeheader(); w.writerows(summary)
    print(f"저장: {os.path.basename(_OUT_EDGES)}, {os.path.basename(_OUT_SUMMARY)}")
