"""
getUnloadRcd(하역정보) 품명 컬럼 확인 도구

목적: 하역정보에 화물 식별에 쓸 품명/품목 컬럼이 있는지, 있다면 구체 화물명인지
카테고리 수준인지 확인한다. 이 하나로 bzentyCd 블로커 우회 가능 여부가 확정된다.

원격(웹)으로는 UPA API 명세가 세션 뒤에 있어 필드를 확정할 수 없다. 대신 팀이 이미
확보한 데이터(raw JSON 또는 upa_unload_record)로 직접 확인한다.

사용법:
  python check_unload_cargo.py <raw.json 또는 export.csv>
  - JSON: getUnloadRcd 응답 원본 (response.body.items.item 구조 자동 인식)
  - CSV : upa_unload_record 를 export 한 파일

Postgres에서 바로 볼 경우:
  \\d upa_unload_record                          -- 컬럼 목록
  SELECT * FROM upa_unload_record LIMIT 5;        -- 샘플
  SELECT DISTINCT <품명컬럼> FROM upa_unload_record;  -- 해상도(구체명 vs 카테고리)

판정 기준:
  - 품명 컬럼이 있고 distinct 값이 '자일렌','메탄올'처럼 구체적 -> MSDS 매칭 소스로 사용 가능
  - 품명이 '액체화학','유류'처럼 카테고리뿐 -> 화물 식별엔 부족, getIntgCag 또는 위험물신고 필요
  - 품명 컬럼 자체가 없음 -> 하역정보는 화물 식별 소스 아님(시각/톤수 위주)
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter

CARGO_KEYS = ["품명", "품목", "화물", "품종", "cargo", "item", "prod", "goods", "cmdt", "prlt"]


def rows_from_json(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    node = d
    for k in ("response", "body", "items", "item"):
        if isinstance(node, dict) and k in node:
            node = node[k]
    if isinstance(node, dict):
        node = [node]
    if not isinstance(node, list):
        raise ValueError("응답에서 item 리스트를 찾지 못함. 구조를 직접 확인 필요.")
    return node


def rows_from_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main(path):
    rows = rows_from_json(path) if path.lower().endswith(".json") else rows_from_csv(path)
    if not rows:
        print("행이 없습니다."); return
    cols = list(rows[0].keys())
    print(f"총 {len(rows)}행, 컬럼 {len(cols)}개")
    print("컬럼:", cols, "\n")

    cargo_cols = [c for c in cols if any(k.lower() in c.lower() for k in CARGO_KEYS)]
    if not cargo_cols:
        print("[판정] 화물 식별 컬럼 없음 -> 하역정보는 화물 식별 소스 아님(시각/톤수 위주).")
        print("       화물 식별은 getIntgCag(bzentyCd) 또는 위험물 신고(dg_un_no) 필요.")
        return

    print(f"[화물 후보 컬럼] {cargo_cols}\n")
    for c in cargo_cols:
        vals = [str(r.get(c, "")).strip() for r in rows if str(r.get(c, "")).strip()]
        distinct = Counter(vals)
        print(f"  # {c}: 값 {len(vals)}개, distinct {len(distinct)}개")
        for v, n in distinct.most_common(12):
            print(f"      {v}  ({n})")
        # 해상도 힌트
        cats = {"액체화학", "유류", "케미칼", "원유", "화학공업생산품", "석유정제품", "기타"}
        looks_category = len(distinct) <= 8 and any(v in cats for v in distinct)
        print("      -> 해상도:", "카테고리 수준(부족)" if looks_category else "구체 화물명일 가능성(양호)", "\n")

    print("[결론] 위 distinct 값이 구체 화물명이면 MSDS 매칭 소스로 사용, 카테고리뿐이면 부족.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("입력 파일 경로를 주세요: python check_unload_cargo.py <raw.json | export.csv>")
    else:
        main(sys.argv[1])
