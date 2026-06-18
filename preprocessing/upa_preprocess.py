# -*- coding: utf-8 -*-
"""
울산항만공사(UPA) Open API 전처리 파이프라인 (담당: 함현우)

대상 API (6종)
  1. 항만 내 선박 운항정보 (getVtsBaseVslNvgtInfo)  -> upa_port_call_stg.csv
  2. 항내 선박위치정보      (getVslPstnInfo)          -> upa_vessel_position_stg.csv
  3-1. 통합화물 정보        (getIntgCagInfo)          -> upa_cargo_manifest_stg.csv
  3-2. 내항화물 정보        (getInprtCagDclrInfo)     -> upa_cargo_manifest_stg.csv (concat)
  4. 선박 하역정보          (getUnloadRcdInfo)        -> upa_unload_record_stg.csv
  5-1. 부두(항만시설) 정보  (getGisBaseHrbrFcltDtlInfo) -> upa_berth_facility_stg.csv
  5-2. 정박지 정보          (getGisBaseAnchrgDtlInfo)   -> upa_anchorage_stg.csv

공통 전처리 적용 순서(기준 문서 8장)를 그대로 따른다.
  raw 로드 -> 컬럼 표준화 -> 결측 표준화 -> 메타컬럼 -> 숫자형 -> 시간(KST->UTC)
  -> port_call_id -> MISSING_KEY -> 좌표 bbox -> 속도/침로 -> 흘수 -> 날짜순서 -> 저장
"""
import os
import json

import numpy as np
import pandas as pd

from . import common
from . import upa_config as cfg


# ===========================================================================
# raw 로더
# ===========================================================================
def load_upa_json(path: str) -> pd.DataFrame:
    """
    공공데이터포털(data.go.kr) 응답 JSON 을 DataFrame 으로 로드한다.

    표준 응답 구조:
        {"response": {"header": {...},
                      "body": {"items": {"item": [...]},
                               "numOfRows": .., "pageNo": .., "totalCount": ..}}}
    items 가 단건이면 dict, 다건이면 list 로 오는 경우를 모두 처리한다.
    이미 list 형태로 저장된 raw(JSON array) 도 그대로 허용한다.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = _extract_items(data)
    return pd.DataFrame(items)


def _extract_items(data):
    """응답 딕셔너리에서 item 리스트를 안전하게 추출."""
    # 이미 list 인 경우
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    # data.go.kr 표준 구조
    body = data.get("response", {}).get("body", data.get("body", data))
    items = body.get("items", body) if isinstance(body, dict) else body

    if isinstance(items, dict):
        item = items.get("item", items)
    else:
        item = items

    if item is None:
        return []
    if isinstance(item, dict):
        return [item]
    if isinstance(item, list):
        return item
    return []


def load_many(paths: list) -> pd.DataFrame:
    """여러 페이지의 raw JSON 을 하나의 DataFrame 으로 합친다."""
    frames = [load_upa_json(p) for p in paths]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ===========================================================================
# UPA 전용 추가 검증
# ===========================================================================
def validate_draught(df: pd.DataFrame, max_draught: float = 30.0) -> pd.DataFrame:
    """
    흘수(draught) 검증. 음수이거나 비현실적으로 큰 값은 INVALID_DRAUGHT 로 표시.
    (공통 기준 문서 5장 품질 플래그 INVALID_DRAUGHT 대응)
    """
    df = df.copy()
    if "draught" in df.columns:
        invalid = (df["draught"] < 0) | (df["draught"] > max_draught)
        df.loc[invalid & df["draught"].notna(), "quality_flag"] = "INVALID_DRAUGHT"
    return df


# ===========================================================================
# 공통 전처리 단계 (모든 API 공유)
# ===========================================================================
def drop_duplicate_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    완전 중복 행 제거.
    (실행마다 바뀌는 collected_at_utc 는 비교에서 제외하여, 같은 원본 행이
     여러 번 들어와도 1건만 남긴다. 운항정보는 이벤트 단위라 정상적으로 여러 행이
     같은 port_call_id 를 가질 수 있으므로, 키가 아닌 '전체 컬럼' 기준으로만 제거한다.)
    """
    df = df.copy()
    subset = [c for c in df.columns if c != "collected_at_utc"]
    before = len(df)
    df = df.drop_duplicates(subset=subset).reset_index(drop=True)
    removed = before - len(df)
    if removed:
        print(f"  - 중복 {removed}행 제거 ({before} -> {len(df)})")
    return df


def _apply_common(df: pd.DataFrame, spec: dict, is_synthetic: bool) -> pd.DataFrame:
    """컬럼 표준화 -> 결측 표준화 -> 메타컬럼 -> 숫자형 -> 시간(KST->UTC)."""
    df = common.standardize_column_names(df, spec["column_map"])
    df = common.normalize_nulls(df)
    df = common.add_common_metadata(
        df,
        source_system=cfg.SOURCE_SYSTEM,
        source_table=spec["source_table"],
        is_synthetic=is_synthetic,
    )
    df = common.to_numeric_safe(df, spec.get("numeric_cols", []))
    df = common.parse_datetime_kst_to_utc(df, spec.get("utc_cols", []))
    return df


# ===========================================================================
# API별 파이프라인
# ===========================================================================
def preprocess_vessel_nvgt(df: pd.DataFrame, is_synthetic: bool = False) -> pd.DataFrame:
    """1. 항만 내 선박 운항정보 -> 입항 건(port call)."""
    spec = cfg.VSL_NVGT_INFO
    df = _apply_common(df, spec, is_synthetic)

    # 이 API는 ptentVtsYr(입항관제연도)만 있으므로 port_call_id용 ptent_yr 로 매핑
    if "ptent_yr" not in df.columns and "ptent_vts_yr" in df.columns:
        df["ptent_yr"] = df["ptent_vts_yr"]

    df = common.create_port_call_id(df)
    df = common.flag_missing_key(df, spec["key_cols"])
    df = common.validate_date_order(df, "arrival_at_utc", "departure_at_utc")
    df = drop_duplicate_rows(df)
    return df


def preprocess_vessel_position(df: pd.DataFrame, is_synthetic: bool = False) -> pd.DataFrame:
    """2. 항내 선박위치정보 -> 선박 위치."""
    spec = cfg.VSL_PSTN_INFO
    df = _apply_common(df, spec, is_synthetic)
    df = common.create_port_call_id(df)
    df = common.flag_missing_key(df, spec["key_cols"])
    df = common.flag_ulsan_bbox(df)
    df = common.validate_speed_course(df)
    df = validate_draught(df)
    df = drop_duplicate_rows(df)
    return df


def preprocess_intg_cargo(df: pd.DataFrame, is_synthetic: bool = False) -> pd.DataFrame:
    """3-1. 통합화물 정보 -> 화물 manifest."""
    spec = cfg.INTG_CAG_INFO
    df = _apply_common(df, spec, is_synthetic)
    df = common.create_port_call_id(df)
    df = common.flag_missing_key(df, spec["key_cols"])
    df = _flag_missing_cargo_name(df)
    return df


def preprocess_inprt_cargo(df: pd.DataFrame, is_synthetic: bool = False) -> pd.DataFrame:
    """3-2. 내항화물 정보 -> 화물 manifest (내항)."""
    spec = cfg.INPRT_CAG_DCLR_INFO
    df = _apply_common(df, spec, is_synthetic)
    df = common.create_port_call_id(df)
    df = common.flag_missing_key(df, spec["key_cols"])
    return df


def preprocess_unload_record(df: pd.DataFrame, is_synthetic: bool = False) -> pd.DataFrame:
    """4. 선박 하역정보 -> 하역 기록."""
    spec = cfg.UNLOAD_RCD_INFO
    df = _apply_common(df, spec, is_synthetic)
    df = common.flag_missing_key(df, spec["key_cols"])
    for start_col, end_col in spec.get("date_order_pairs", []):
        df = common.validate_date_order(df, start_col, end_col)
    return df


def preprocess_berth_facility(df: pd.DataFrame, is_synthetic: bool = False) -> pd.DataFrame:
    """5-1. 부두(항만시설) 정보 -> 부두."""
    spec = cfg.HRBR_FCLT_INFO
    df = _apply_common(df, spec, is_synthetic)
    df = common.flag_missing_key(df, spec["key_cols"])
    df = common.flag_ulsan_bbox(df)
    return df


def preprocess_anchorage(df: pd.DataFrame, is_synthetic: bool = False) -> pd.DataFrame:
    """5-2. 정박지 정보 -> 정박지."""
    spec = cfg.ANCHRG_INFO
    df = _apply_common(df, spec, is_synthetic)
    df = common.flag_missing_key(df, spec["key_cols"])
    df = common.flag_ulsan_bbox(df)
    return df


def _flag_missing_cargo_name(df: pd.DataFrame) -> pd.DataFrame:
    """화물명(cargo_name_raw) 결측 시 MISSING_CARGO_NAME 표시 (기존 OK 인 행만)."""
    df = df.copy()
    if "cargo_name_raw" in df.columns:
        miss = df["cargo_name_raw"].isna() & (df["quality_flag"] == "OK")
        df.loc[miss, "quality_flag"] = "MISSING_CARGO_NAME"
    return df


# ===========================================================================
# 엔드투엔드 실행 (raw 경로 -> staging 저장)
# ===========================================================================
# API key -> (preprocess 함수, config spec)
PIPELINES = {
    "vessel_nvgt": (preprocess_vessel_nvgt, cfg.VSL_NVGT_INFO),
    "vessel_position": (preprocess_vessel_position, cfg.VSL_PSTN_INFO),
    "intg_cargo": (preprocess_intg_cargo, cfg.INTG_CAG_INFO),
    "inprt_cargo": (preprocess_inprt_cargo, cfg.INPRT_CAG_DCLR_INFO),
    "unload_record": (preprocess_unload_record, cfg.UNLOAD_RCD_INFO),
    "berth_facility": (preprocess_berth_facility, cfg.HRBR_FCLT_INFO),
    "anchorage": (preprocess_anchorage, cfg.ANCHRG_INFO),
}


def run_pipeline(
    api_key: str,
    raw_paths: list,
    staging_dir: str = "data/staging",
    is_synthetic: bool = False,
    save: bool = True,
) -> pd.DataFrame:
    """
    단일 API 전처리 실행.

    Parameters
    ----------
    api_key : PIPELINES 의 키 ("vessel_position", "intg_cargo" 등)
    raw_paths : raw JSON 파일 경로 리스트
    staging_dir : staging 저장 폴더
    is_synthetic : 가상 데이터 여부
    save : True 면 staging CSV 저장
    """
    if api_key not in PIPELINES:
        raise ValueError(f"Unknown api_key: {api_key}. choices={list(PIPELINES)}")

    func, spec = PIPELINES[api_key]
    raw = load_many(raw_paths)
    if raw.empty:
        print(f"[WARN] {api_key}: raw 데이터가 비어 있습니다 -> {raw_paths}")
        return pd.DataFrame()

    df = func(raw, is_synthetic=is_synthetic)

    if save:
        os.makedirs(staging_dir, exist_ok=True)
        out = os.path.join(staging_dir, spec["stg_name"])
        common.save_staging_csv(df, out)
        print(f"[OK] {api_key}: {len(df)} rows -> {out}")
    return df


def run_cargo_combined(
    intg_paths: list,
    inprt_paths: list,
    staging_dir: str = "data/staging",
    is_synthetic: bool = False,
) -> pd.DataFrame:
    """
    통합화물(3-1) + 내항화물(3-2) 을 하나의 upa_cargo_manifest_stg.csv 로 결합.
    공통 컬럼은 정렬되고, 한쪽에만 있는 컬럼은 NaN 으로 채워진다.
    """
    frames = []
    if intg_paths:
        raw = load_many(intg_paths)
        if not raw.empty:
            frames.append(preprocess_intg_cargo(raw, is_synthetic=is_synthetic))
    if inprt_paths:
        raw = load_many(inprt_paths)
        if not raw.empty:
            frames.append(preprocess_inprt_cargo(raw, is_synthetic=is_synthetic))

    if not frames:
        print("[WARN] cargo: raw 데이터가 비어 있습니다")
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True, sort=False)
    os.makedirs(staging_dir, exist_ok=True)
    out = os.path.join(staging_dir, cfg.INTG_CAG_INFO["stg_name"])
    common.save_staging_csv(df, out)
    print(f"[OK] cargo(통합+내항): {len(df)} rows -> {out}")
    return df


if __name__ == "__main__":
    # 사용 예시 (실제 raw 파일 경로로 교체)
    base = "data/raw/upa"
    examples = {
        "vessel_nvgt": [f"{base}/upa_vessel_nvgt_raw.json"],
        "vessel_position": [f"{base}/upa_vessel_position_raw.json"],
        "unload_record": [f"{base}/upa_unload_record_raw.json"],
        "berth_facility": [f"{base}/upa_berth_facility_raw.json"],
        "anchorage": [f"{base}/upa_anchorage_raw.json"],
    }
    for key, paths in examples.items():
        if all(os.path.exists(p) for p in paths):
            run_pipeline(key, paths)

    intg = [f"{base}/upa_intg_cargo_raw.json"]
    inprt = [f"{base}/upa_inprt_cargo_raw.json"]
    if os.path.exists(intg[0]) or os.path.exists(inprt[0]):
        run_cargo_combined(
            intg_paths=[p for p in intg if os.path.exists(p)],
            inprt_paths=[p for p in inprt if os.path.exists(p)],
        )
