"""
선석 좌표 채우기 도구 (GAP 1 마무리)

onsan_berth_master.csv 의 빈 lat/lon 을 채운다. 좌표 입력원은 두 가지:
1. 기존 수집 데이터: upa_berth_facility(getGisHrbr 수집분)에서 선석명, 위경도를 export
2. 입항정보 PDF: 각 터미널 PDF 1.1(부두 상세)의 위치 좌표 (DMS 표기)

두 소스 모두 berth_coords_input.csv 한 장으로 받아서 병합한다.
lat_raw/lon_raw 는 DMS("35°27'23.8\"N")도, 십진수("35.4566")도 자동 인식한다.

순서 권고:
  (1) 먼저 upa_berth_facility 에 온산 대상 선석 좌표가 이미 있는지 확인
      SQL 예: SELECT wharf_name, lat, lon FROM upa_berth_facility
              WHERE wharf_name LIKE '%정일%' OR wharf_name LIKE '%OTK%'
                 OR wharf_name LIKE '%대한유화%' OR wharf_name LIKE '%효성%'
                 OR wharf_name LIKE '%S-Oil%' OR wharf_name LIKE '%온산%';
  (2) 있으면 berth_coords_input.csv 에 옮겨 적고, 없는 것만 입항정보 PDF 1.1 에서 보충
  (3) 이 스크립트 실행 -> onsan_berth_master.csv 갱신 + 남은 결측 보고
"""

from __future__ import annotations

import csv
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = next((d for d in [os.path.join(_HERE, "..", "data"), _HERE] if os.path.isdir(d)), _HERE)
_MASTER = os.path.join(_DATA, "onsan_berth_master.csv")
_INPUT = os.path.join(_DATA, "berth_coords_input.csv")

_HEMI = {"N": 1, "S": -1, "E": 1, "W": -1}


def parse_coord(raw: str):
    """DMS 또는 십진수 문자열 -> float(십진도). 못 읽으면 None.
    허용 예: '35°27\\'23.8\"N', 'N 35 27 23.8', '129°22\\'01\"E', '35.4566', '-35.4566'
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None

    hemi = 1
    m = re.search(r"[NSEW]", s, re.IGNORECASE)
    if m:
        hemi = _HEMI[m.group(0).upper()]
        s = re.sub(r"[NSEW]", " ", s, flags=re.IGNORECASE)

    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    if not nums:
        return None

    # 십진수 단일값 (도만 있고 분/초 없음)
    if len(nums) == 1:
        val = float(nums[0])
        if val < 0:
            return val  # 이미 부호 있는 십진수
        return val * hemi

    deg = float(nums[0])
    minutes = float(nums[1]) if len(nums) > 1 else 0.0
    seconds = float(nums[2]) if len(nums) > 2 else 0.0
    dec = deg + minutes / 60.0 + seconds / 3600.0
    return round(dec * hemi, 6)


def load_input(path: str = _INPUT) -> dict:
    """berth_coords_input.csv -> {berth_id: (lat, lon, source)}"""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            bid = (r.get("berth_id") or "").strip()
            if not bid:
                continue
            lat = parse_coord(r.get("lat_raw"))
            lon = parse_coord(r.get("lon_raw"))
            if lat is not None and lon is not None:
                out[bid] = (lat, lon, (r.get("source") or "").strip())
    return out


def fill_master(master_path: str = _MASTER, input_path: str = _INPUT):
    coords = load_input(input_path)

    with open(master_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)

    updated, still_missing = [], []
    for row in rows:
        bid = (row.get("berth_id") or "").strip()
        has = (row.get("lat") or "").strip() and (row.get("lon") or "").strip()
        if not has and bid in coords:
            lat, lon, src = coords[bid]
            row["lat"] = f"{lat:.6f}"
            row["lon"] = f"{lon:.6f}"
            if src:
                row["coord_source"] = src
            updated.append(bid)
        elif not has:
            still_missing.append(bid)

    with open(master_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    return updated, still_missing


if __name__ == "__main__":
    # 파서 자체 검증 (정일/OTK PDF 표기 그대로)
    tests = [
        ("35° 27' 23.8\"N", 35.456611),
        ("129° 21' 4.3\"E", 129.351194),
        ("N 35° 26' 16\"", 35.437778),
        ("E 129° 22' 01\"", 129.366944),
        ("35.4566", 35.4566),
    ]
    print("=== 좌표 파서 검증 ===")
    for raw, exp in tests:
        got = parse_coord(raw)
        ok = "OK" if abs(got - exp) < 1e-3 else "FAIL"
        print(f"  [{ok}] {raw!r:22s} -> {got} (기대 {exp})")
    print()

    updated, missing = fill_master()
    print(f"=== 병합 결과 ===")
    print(f"좌표 채운 선석 {len(updated)}개: {updated}")
    print(f"아직 결측 {len(missing)}개: {missing}")
