"""
온산 MVP 선석 인접관계(ADJACENT_TO) 생성 (GAP 1)

현재 Neo4j 의 ADJACENT_TO 14건은 이전 5개 파일럿 선석군(정일, OTK, 현대오일신항,
SK, 북신항에너지)에 수동 입력돼 있다. 온산 2클러스터(처용리+산암리)로 스코프를
바꾸면 정일/OTK만 살아남고 나머지는 스코프 밖으로 빠지며, 새로 들어온 UTK/대한유화/
효성/S-Oil 등은 인접 엣지가 없다. 이 스크립트는 좌표 기반으로 인접관계를 다시 만든다.

동작:
1. onsan_berth_master.csv 에서 좌표가 있는 선석쌍의 haversine 거리를 계산
2. 임계 거리 이내면 ADJACENT_TO 로 연결 (양방향)
3. 좌표 결측 선석은 경고로 출력 (getGisHrbr 확보 후 재실행 대상)
4. Neo4j Cypher MERGE 문과 엣지 CSV 를 생성

인접은 수역 소속이 아니라 실제 좌표 거리로 판정한다. 처용리와 산암리는 약 2.5km
떨어져 있어 임계 500m 로는 자연히 클러스터를 넘지 않는다.
"""

from __future__ import annotations

import csv
import math
import os

THRESHOLD_M = 500.0  # 인접 판정 거리 (m). 같은 터미널 선석은 대표점 공유로 0m 처리됨.

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = next((d for d in [os.path.join(_HERE, "..", "data"), _HERE] if os.path.isdir(d)), _HERE)
_MASTER_CSV = os.path.join(_DATA, "onsan_berth_master.csv")
_EDGE_CSV_OUT = os.path.join(_DATA, "onsan_adjacency_edges.csv")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 위경도 사이 거리(m)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _f(v):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_berths(csv_path: str = _MASTER_CSV) -> list:
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append({
                "berth_id": (r.get("berth_id") or "").strip(),
                "berth_name": (r.get("berth_name") or "").strip(),
                "수역": (r.get("수역") or "").strip(),
                "operator": (r.get("operator") or "").strip(),
                "cargo_class": (r.get("cargo_class") or "").strip(),
                "tier": (r.get("tier") or "").strip(),
                "lat": _f(r.get("lat")),
                "lon": _f(r.get("lon")),
            })
    return rows


def build_edges(berths: list, threshold_m: float = THRESHOLD_M):
    """좌표가 있는 선석쌍만 거리 계산. (edges, missing_coord_berths) 반환."""
    have = [b for b in berths if b["lat"] is not None and b["lon"] is not None]
    missing = [b for b in berths if b["lat"] is None or b["lon"] is None]

    edges = []
    for i in range(len(have)):
        for j in range(i + 1, len(have)):
            a, b = have[i], have[j]
            d = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            if d <= threshold_m:
                edges.append({
                    "a": a["berth_id"], "b": b["berth_id"],
                    "수역_a": a["수역"], "수역_b": b["수역"],
                    "distance_m": round(d, 1),
                })
    return edges, missing


def to_cypher(edges: list) -> str:
    """ADJACENT_TO MERGE 문 (양방향). Berth 노드가 berth_id 로 존재한다고 가정."""
    lines = []
    for e in edges:
        lines.append(
            f"MATCH (a:Berth {{berth_id: '{e['a']}'}}), (b:Berth {{berth_id: '{e['b']}'}})\n"
            f"MERGE (a)-[:ADJACENT_TO {{distance_m: {e['distance_m']}}}]->(b)\n"
            f"MERGE (b)-[:ADJACENT_TO {{distance_m: {e['distance_m']}}}]->(a);"
        )
    return "\n".join(lines)


def mvp_berth_ids(csv_path: str = _MASTER_CSV) -> set:
    """스케줄링 후보풀 제한용. 스케줄링 에이전트에서 후보를 이 집합으로 필터링한다.
    예: candidates = [c for c in candidates if c.berth_id in mvp_berth_ids()]"""
    return {b["berth_id"] for b in load_berths(csv_path) if b["berth_id"]}


if __name__ == "__main__":
    berths = load_berths()
    edges, missing = build_edges(berths)

    print(f"선석 총 {len(berths)}개, 좌표 보유 {len(berths) - len(missing)}개, 결측 {len(missing)}개")
    print(f"인접 임계 {THRESHOLD_M:.0f} m")
    print()

    print("=== 생성된 ADJACENT_TO 엣지 (양방향 1쌍 표기) ===")
    if edges:
        for e in edges:
            same = "동일수역" if e["수역_a"] == e["수역_b"] else "수역교차(검토)"
            print(f"  {e['a']} <-> {e['b']}  ({e['distance_m']} m, {same})")
    else:
        print("  (없음)")
    print()

    print("=== 좌표 결측 선석 (getGisHrbr 확보 후 재실행 대상) ===")
    for b in missing:
        print(f"  {b['berth_id']:10s} {b['berth_name']} [{b['수역']}]")
    print()

    # 엣지 CSV 저장
    with open(_EDGE_CSV_OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["a", "b", "수역_a", "수역_b", "distance_m"])
        w.writeheader()
        w.writerows(edges)
    print(f"엣지 CSV 저장: {os.path.basename(_EDGE_CSV_OUT)}")
    print()

    print("=== Neo4j Cypher ===")
    print(to_cypher(edges) or "(엣지 없음)")
    print()

    print("=== 스케줄링 후보풀 제한용 berth_id 집합 ===")
    print(sorted(mvp_berth_ids()))
