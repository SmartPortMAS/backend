# -*- coding: utf-8 -*-
"""
울산항 좌표 범위(bbox) 진단 스크립트

선박위치/부두/정박지 staging 의 실제 좌표 분포를 출력하여,
공통 기준의 울산항 bbox(common.py 의 ULSAN_* 상수)가 적절한지 팀이 판단하도록 돕는다.

주의: bbox 상수는 3팀 공통(common.py)이므로 변경은 팀 합의 후 함께 수정한다.
실행: python -m backend.preprocessing.check_coordinates
"""
import glob
import os

import pandas as pd

from .common import (
    ULSAN_LAT_MIN, ULSAN_LAT_MAX, ULSAN_LON_MIN, ULSAN_LON_MAX,
)

STAGING_DIR = "data/staging"
COORD_FILES = [
    "upa_vessel_position_stg.csv",
    "upa_berth_facility_stg.csv",
    "upa_anchorage_stg.csv",
]


def describe_coords(path: str) -> None:
    df = pd.read_csv(path)
    if "latitude" not in df.columns or "longitude" not in df.columns:
        print(f"[SKIP] {os.path.basename(path)} 좌표 컬럼 없음")
        return

    lat = pd.to_numeric(df["latitude"], errors="coerce")
    lon = pd.to_numeric(df["longitude"], errors="coerce")
    valid = lat.notna() & lon.notna() & (lat != 0) & (lon != 0)
    lat, lon = lat[valid], lon[valid]
    n = len(lat)
    if n == 0:
        print(f"[{os.path.basename(path)}] 유효 좌표 없음")
        return

    in_box = (
        lat.between(ULSAN_LAT_MIN, ULSAN_LAT_MAX)
        & lon.between(ULSAN_LON_MIN, ULSAN_LON_MAX)
    ).sum()

    print(f"\n[{os.path.basename(path)}] 유효좌표 {n}건")
    print(f"  위도(lat): min={lat.min():.4f}  max={lat.max():.4f}  "
          f"p5={lat.quantile(.05):.4f}  p95={lat.quantile(.95):.4f}")
    print(f"  경도(lon): min={lon.min():.4f}  max={lon.max():.4f}  "
          f"p5={lon.quantile(.05):.4f}  p95={lon.quantile(.95):.4f}")
    print(f"  현재 bbox 안: {in_box}/{n} ({in_box/n*100:.1f}%)  "
          f"[현재 bbox: lat {ULSAN_LAT_MIN}~{ULSAN_LAT_MAX} / lon {ULSAN_LON_MIN}~{ULSAN_LON_MAX}]")
    # 데이터 99% 를 담는 추천 박스 (참고용)
    print(f"  ▶ 데이터 99% 포함 추천 박스: "
          f"lat {lat.quantile(.005):.3f}~{lat.quantile(.995):.3f} / "
          f"lon {lon.quantile(.005):.3f}~{lon.quantile(.995):.3f}")


def main() -> None:
    files = [os.path.join(STAGING_DIR, f) for f in COORD_FILES]
    files = [f for f in files if os.path.exists(f)]
    if not files:
        print(f"{STAGING_DIR} 에 좌표 staging 이 없습니다. 먼저 전처리를 실행하세요.")
        return
    print("=== 울산항 좌표 범위 진단 ===")
    for f in files:
        describe_coords(f)
    print("\n※ '추천 박스'와 현재 bbox를 비교해 팀과 함께 common.py 의 ULSAN_* 값을 조정하세요.")


if __name__ == "__main__":
    main()
