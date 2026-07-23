"""
온산 MVP Neo4j 그래프 적재 (P1)

onsan_mvp/data 의 시드 CSV를 Neo4j에 적재한다.
- Berth 노드: onsan_berth_master.csv (온산 스코프 15시설, 좌표/수역/운영사/선석수 속성)
- ADJACENT_TO: onsan_adjacency_edges.csv (기존 파일럿 엣지 전량 대체, 양방향, 혼재/근접 위험용)
- SUBSTITUTABLE_WITH: onsan_berth_substitutability.csv (방향성, 선석 대체용. ADJACENT_TO 와 별도 관계)

접속정보는 환경변수로만 읽는다 (하드코딩 금지):
  NEO4J_URI (기본 bolt://localhost:7687), NEO4J_USER (기본 neo4j), NEO4J_PASSWORD (필수)

실행:
  python backend/graph/load_onsan_graph.py            # 실제 적재
  python backend/graph/load_onsan_graph.py --dry-run  # CSV 검증 + 적재 계획만 출력 (Neo4j 불필요)

좌표 결측 3개(달포, S-Oil 부이, 오일허브 부이)는 coord_missing=true 로 표시만 하고 적재한다.
좌표 확보 후 build_adjacency.py 재실행 -> 이 스크립트 재실행 (MERGE 라 멱등).
"""
from __future__ import annotations

import csv
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_ONSAN_SCRIPTS = os.path.join(_REPO_ROOT, "onsan_mvp", "scripts")
_ONSAN_DATA = os.path.join(_REPO_ROOT, "onsan_mvp", "data")
if _ONSAN_SCRIPTS not in sys.path:
    sys.path.insert(0, _ONSAN_SCRIPTS)

from build_adjacency import load_berths, mvp_berth_ids  # noqa: E402

_MASTER_CSV = os.path.join(_ONSAN_DATA, "onsan_berth_master.csv")
_ADJ_CSV = os.path.join(_ONSAN_DATA, "onsan_adjacency_edges.csv")
_SUB_CSV = os.path.join(_ONSAN_DATA, "onsan_berth_substitutability.csv")
_ANCHOR_CSV = os.path.join(_ONSAN_DATA, "onsan_anchorage.csv")
_FALLBACK_CSV = os.path.join(_ONSAN_DATA, "onsan_berth_anchorage_fallback.csv")


def _num(v):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return None


def read_berth_rows() -> list:
    """Berth 노드 속성 행. load_berths() 가 안 싣는 제원 컬럼은 CSV에서 보충한다."""
    base = {b["berth_id"]: b for b in load_berths(_MASTER_CSV)}
    rows = []
    with open(_MASTER_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            berth_id = (r.get("berth_id") or "").strip()
            b = base.get(berth_id)
            if not b:
                continue
            rows.append({
                "berth_id": berth_id,
                "props": {
                    "berth_name": b["berth_name"],
                    "waterway": b["수역"],
                    "operator": b["operator"],
                    "cargo_class": b["cargo_class"],
                    "tier": b["tier"],
                    "lat": b["lat"],
                    "lon": b["lon"],
                    "coord_missing": b["lat"] is None or b["lon"] is None,
                    "length_m": _num(r.get("길이_m")),
                    "depth_m": _num(r.get("수심_m")),
                    "capacity_dwt": _num(r.get("접안능력_DWT")),
                    "berth_count": _num(r.get("선석수")),
                    "max_draught_m": _num(r.get("최대흘수_m")),
                    "cargo_types": (r.get("취급화물") or "").strip(),
                    "coord_source": (r.get("coord_source") or "").strip(),
                    "mvp_scope": True,
                },
            })
    return rows


def read_adjacency_rows() -> list:
    with open(_ADJ_CSV, encoding="utf-8-sig") as f:
        return [
            {"a": r["a"].strip(), "b": r["b"].strip(), "distance_m": float(r["distance_m"])}
            for r in csv.DictReader(f)
        ]


def read_substitutability_rows() -> list:
    with open(_SUB_CSV, encoding="utf-8-sig") as f:
        return [
            {
                "from_berth": r["from_berth"].strip(),
                "to_berth": r["to_berth"].strip(),
                "operator": (r.get("operator") or "").strip(),
                "shared_products": (r.get("shared_products") or "").strip(),
                "to_max_dwt": _num(r.get("to_max_dwt")),
                "to_depth_m": _num(r.get("to_depth_m")),
            }
            for r in csv.DictReader(f)
        ]


def read_anchorage_rows() -> list:
    with open(_ANCHOR_CSV, encoding="utf-8-sig") as f:
        return [
            {
                "anchorage_id": r["anchorage_id"].strip(),
                "props": {
                    "name": (r.get("name") or "").strip(),
                    "type": (r.get("type") or "").strip(),
                    "tonnage_rule": (r.get("tonnage_rule") or "").strip(),
                    "lat": _num(r.get("lat")),
                    "lon": _num(r.get("lon")),
                    "radius_note": (r.get("radius_note") or "").strip(),
                    "source": (r.get("source") or "").strip(),
                },
            }
            for r in csv.DictReader(f)
        ]


def read_fallback_rows(anchorage_ids: set) -> tuple:
    """(정박지 엣지 행, 정박지 노드가 없는 폴백[부이직접/외해대기] 행) 분리."""
    edges, notes = [], []
    with open(_FALLBACK_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            row = {
                "berth_id": r["berth_id"].strip(),
                "fallback": (r.get("fallback_anchorage") or "").strip(),
            }
            (edges if row["fallback"] in anchorage_ids else notes).append(row)
    return edges, notes


def load_to_neo4j(berths: list, adj: list, subs: list, anchors: list,
                  fb_edges: list, fb_notes: list) -> dict:
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise SystemExit("NEO4J_PASSWORD 환경변수가 필요합니다 (docker-compose 값 참조). 하드코딩 금지.")

    ids = sorted(mvp_berth_ids(_MASTER_CSV))
    stats = {}
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as s:
            s.run("CREATE CONSTRAINT berth_id_unique IF NOT EXISTS "
                  "FOR (b:Berth) REQUIRE b.berth_id IS UNIQUE").consume()

            r = s.run(
                "UNWIND $rows AS row "
                "MERGE (b:Berth {berth_id: row.berth_id}) SET b += row.props",
                rows=berths,
            ).consume()
            stats["berth_nodes"] = len(berths)

            # 온산 스코프 밖 기존 Berth 제거 (berth_id 가 없는 구 파일럿 노드 포함)
            r = s.run(
                "MATCH (b:Berth) WHERE b.berth_id IS NULL OR NOT b.berth_id IN $ids "
                "DETACH DELETE b RETURN count(*) AS n",
                ids=ids,
            )
            stats["out_of_scope_deleted"] = r.single()["n"]

            # ADJACENT_TO 전량 대체 (양방향)
            s.run("MATCH ()-[r:ADJACENT_TO]->() DELETE r").consume()
            s.run(
                "UNWIND $rows AS row "
                "MATCH (a:Berth {berth_id: row.a}), (b:Berth {berth_id: row.b}) "
                "MERGE (a)-[r1:ADJACENT_TO]->(b) SET r1.distance_m = row.distance_m "
                "MERGE (b)-[r2:ADJACENT_TO]->(a) SET r2.distance_m = row.distance_m",
                rows=adj,
            ).consume()
            stats["adjacent_to_directed"] = len(adj) * 2

            # SUBSTITUTABLE_WITH 전량 대체 (방향성 유지, ADJACENT_TO 와 별도)
            s.run("MATCH ()-[r:SUBSTITUTABLE_WITH]->() DELETE r").consume()
            s.run(
                "UNWIND $rows AS row "
                "MATCH (a:Berth {berth_id: row.from_berth}), (b:Berth {berth_id: row.to_berth}) "
                "MERGE (a)-[r:SUBSTITUTABLE_WITH]->(b) "
                "SET r.operator = row.operator, r.shared_products = row.shared_products, "
                "    r.to_max_dwt = row.to_max_dwt, r.to_depth_m = row.to_depth_m",
                rows=subs,
            ).consume()
            stats["substitutable_with_directed"] = len(subs)

            # Anchorage 노드 (E1/E2/E3/W1/B1~B3)
            s.run("CREATE CONSTRAINT anchorage_id_unique IF NOT EXISTS "
                  "FOR (a:Anchorage) REQUIRE a.anchorage_id IS UNIQUE").consume()
            s.run(
                "UNWIND $rows AS row "
                "MERGE (a:Anchorage {anchorage_id: row.anchorage_id}) SET a += row.props",
                rows=anchors,
            ).consume()
            stats["anchorage_nodes"] = len(anchors)

            # FALLBACK_ANCHORAGE 전량 대체
            s.run("MATCH ()-[r:FALLBACK_ANCHORAGE]->() DELETE r").consume()
            s.run(
                "UNWIND $rows AS row "
                "MATCH (b:Berth {berth_id: row.berth_id}), (a:Anchorage {anchorage_id: row.fallback}) "
                "MERGE (b)-[:FALLBACK_ANCHORAGE]->(a)",
                rows=fb_edges,
            ).consume()
            stats["fallback_anchorage_edges"] = len(fb_edges)

            # 정박지 노드가 없는 폴백(VLCC 부이: 부이직접/외해대기)은 노드 속성으로 표시
            s.run(
                "UNWIND $rows AS row "
                "MATCH (b:Berth {berth_id: row.berth_id}) SET b.anchorage_fallback_note = row.fallback",
                rows=fb_notes,
            ).consume()
            stats["fallback_note_only"] = len(fb_notes)
    finally:
        driver.close()
    return stats


def main():
    dry_run = "--dry-run" in sys.argv

    berths = read_berth_rows()
    adj = read_adjacency_rows()
    subs = read_substitutability_rows()
    anchors = read_anchorage_rows()
    fb_edges, fb_notes = read_fallback_rows({a["anchorage_id"] for a in anchors})
    missing = [b for b in berths if b["props"]["coord_missing"]]

    print(f"Berth 노드: {len(berths)}개 (온산 MVP 스코프)")
    print(f"  좌표 결측(표시만, 사람 확보 대상): {[b['berth_id'] for b in missing]}")
    print(f"ADJACENT_TO: {len(adj)}쌍 -> 방향성 {len(adj) * 2}개")
    print(f"SUBSTITUTABLE_WITH: {len(subs)}개 (방향성, 별도 관계)")
    print(f"Anchorage 노드: {len(anchors)}개 ({', '.join(a['anchorage_id'] for a in anchors)})")
    print(f"FALLBACK_ANCHORAGE 엣지: {len(fb_edges)}개, 노드 없는 폴백(부이직접/외해대기): "
          f"{[r['berth_id'] for r in fb_notes]}")

    berth_ids = {b["berth_id"] for b in berths}
    bad_adj = [e for e in adj if e["a"] not in berth_ids or e["b"] not in berth_ids]
    bad_sub = [e for e in subs if e["from_berth"] not in berth_ids or e["to_berth"] not in berth_ids]
    bad_fb = [e for e in fb_edges if e["berth_id"] not in berth_ids]
    if bad_adj or bad_sub or bad_fb:
        raise SystemExit(f"CSV 정합성 오류 - 마스터에 없는 berth_id 참조: adj={bad_adj}, sub={bad_sub}, fb={bad_fb}")
    print("CSV 정합성: OK (모든 엣지 endpoint 가 마스터에 존재)")

    if dry_run:
        print("\n[dry-run] Neo4j 접속 없이 종료")
        return

    stats = load_to_neo4j(berths, adj, subs, anchors, fb_edges, fb_notes)
    print("\nNeo4j 적재 완료:", stats)


if __name__ == "__main__":
    main()
