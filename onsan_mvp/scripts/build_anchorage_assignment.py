"""
정박지 배정 규칙 + berth 폴백 매핑 (FALLBACK_ANCHORAGE)

선석이 전용부두라 대체가 운영사 안으로 제한되고, 없으면 정박지 대기다(선석대체_정박지대기_모델 참조).
이 스크립트는 두 가지를 만든다.
1) assign_anchorage(dwt): 선박 톤수 -> 대기 정박지(E1/E2/E3). 스케줄러 런타임에서 사용.
2) berth 폴백 매핑: 각 선석의 최대 접안 DWT 기준으로 그 선석이 막혔을 때 대기할 정박지.
   Berth -[FALLBACK_ANCHORAGE]-> Anchorage 엣지로 적재.

정박지 톤수 기준(onsan_anchorage.csv, 해수부 시설현황):
  E1 1만톤 이하 / E2 3만톤 이하 / E3 2만톤 이상 / W1 2만톤 이하(원형)
배정 로직: dwt<=10000 -> E1, 10000<dwt<=20000 -> E2, dwt>20000 -> E3.
원유 VLCC(부이, 32만~35만톤)는 E 정박지로 수용 불가라 부이 직접/외해 대기로 표시.
"""

from __future__ import annotations

import csv
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = next((d for d in [os.path.join(_HERE, "..", "data"), _HERE] if os.path.isdir(d)), _HERE)
_MASTER = os.path.join(_DATA, "onsan_berth_master.csv")
_ANCHOR = os.path.join(_DATA, "onsan_anchorage.csv")
_OUT = os.path.join(_DATA, "onsan_berth_anchorage_fallback.csv")

VLCC_BUOY_DWT = 150000  # 이 이상 원유는 E 정박지 수용 불가


def _f(v):
    v = (v or "").strip()
    return float(v) if v else None


def assign_anchorage(dwt):
    """선박 재화중량톤수 -> 대기 정박지 id. VLCC급은 None(부이/외해)."""
    if dwt is None:
        return "판단불가"
    if dwt >= VLCC_BUOY_DWT:
        return "부이직접/외해대기"
    if dwt <= 10000:
        return "E1"
    if dwt <= 20000:
        return "E2"
    return "E3"


def load_master(path=_MASTER):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    rows = load_master()
    out = []
    for r in rows:
        bid = (r.get("berth_id") or "").strip()
        dwt = _f(r.get("접안능력_DWT"))
        cls = (r.get("cargo_class") or "").strip()
        anc = assign_anchorage(dwt)
        out.append({
            "berth_id": bid,
            "berth_name": (r.get("berth_name") or "").strip(),
            "max_dwt": "" if dwt is None else int(dwt),
            "fallback_anchorage": anc,
        })

    print(f"{'berth':10s} {'max_dwt':>9s}  fallback")
    for o in out:
        print(f"{o['berth_id']:10s} {str(o['max_dwt']):>9s}  {o['fallback_anchorage']}")

    with open(_OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["berth_id", "berth_name", "max_dwt", "fallback_anchorage"])
        w.writeheader(); w.writerows(out)
    print(f"\n저장: {os.path.basename(_OUT)}")

    print("\n=== Cypher (E 정박지 폴백만) ===")
    for o in out:
        if o["fallback_anchorage"].startswith("E"):
            print(f"MATCH (b:Berth {{berth_id:'{o['berth_id']}'}}),(a:Anchorage {{anchorage_id:'{o['fallback_anchorage']}'}}) "
                  f"MERGE (b)-[:FALLBACK_ANCHORAGE]->(a);")


if __name__ == "__main__":
    # 자체 검증: 톤수별 배정
    print("=== assign_anchorage 검증 ===")
    for d in [3000, 10000, 15000, 30000, 80000, 325000]:
        print(f"  {d:>7} DWT -> {assign_anchorage(d)}")
    print()
    main()
