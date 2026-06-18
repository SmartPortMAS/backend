# -*- coding: utf-8 -*-
"""
울산항 액체화물 관제 시스템 - 공통 전처리 함수

팀 공통 기준 문서(공통 데이터 수집·전처리 기준 문서) 7장의 공통 코드를 그대로
모듈화한 것이다. 3명의 담당자(AIS / 울산항만공사 API / MSDS·기상)가 동일하게
사용하며, 각 데이터별 특수 전처리는 이 공통 함수 이후에 추가한다.
"""
import pandas as pd
import numpy as np

# 공통 결측값 목록
# (API마다 결측값 표현이 다르기 때문에 하나의 기준으로 통일)
NULL_VALUES = ["", " ", "null", "NULL", "None", "none", "-", "N/A", "nan", "NaN"]

# 울산항 1차 관제 범위
# (AIS, 항내 선박위치, 정박지, 부두 위치 검증에 공통 적용)
ULSAN_LAT_MIN = 35.30
ULSAN_LAT_MAX = 35.58
ULSAN_LON_MIN = 129.18
ULSAN_LON_MAX = 129.52


def normalize_nulls(df: pd.DataFrame) -> pd.DataFrame:
    """
    결측값 표준화 함수
    (빈 문자열, 'null', '-', 'N/A' 등 서로 다른 결측 표현을 실제 결측값으로 통일)
    """
    df = df.copy()
    df = df.replace(NULL_VALUES, np.nan)
    return df


def standardize_column_names(df: pd.DataFrame, column_map: dict) -> pd.DataFrame:
    """
    컬럼명 표준화 함수
    (원본 데이터마다 다른 컬럼명을 팀 공통 컬럼명으로 변경)
    예: MMSI, mmsiNo -> mmsi / Latitude, lat -> latitude
    """
    df = df.copy()
    df = df.rename(columns=column_map)
    return df


def add_common_metadata(
    df: pd.DataFrame,
    source_system: str,
    source_table: str,
    is_synthetic: bool = False,
) -> pd.DataFrame:
    """
    공통 메타데이터 추가 함수
    (출처, 원본 테이블명, 수집시각, 품질 플래그, 실제/가상 여부를 모든 데이터에 추가)
    """
    df = df.copy()
    df["source_system"] = source_system
    df["source_table"] = source_table
    df["collected_at_utc"] = pd.Timestamp.utcnow()
    df["quality_flag"] = "OK"
    df["is_synthetic"] = is_synthetic
    return df


def to_numeric_safe(df: pd.DataFrame, columns: list) -> pd.DataFrame:
    """
    숫자형 변환 함수
    (좌표, 속도, 수심, 톤수처럼 문자열로 들어오는 숫자를 float/int로 변환)
    변환이 불가능한 값은 NaN으로 처리
    """
    df = df.copy()
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def parse_datetime_utc(df: pd.DataFrame, columns: list) -> pd.DataFrame:
    """
    UTC 시간 파싱 함수
    (AISStream의 TimeUtc처럼 이미 UTC 기준인 시간을 datetime으로 변환)
    """
    df = df.copy()
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True, format="mixed")
    return df


def parse_datetime_kst_to_utc(df: pd.DataFrame, columns: list) -> pd.DataFrame:
    """
    KST 시간 → UTC 변환 함수
    (울산항만공사 API, 기상청 API처럼 한국시간으로 들어오는 날짜를 UTC로 변환)
    """
    df = df.copy()
    for col in columns:
        if col in df.columns:
            dt = pd.to_datetime(df[col], errors="coerce", format="mixed")
            df[col] = (
                dt
                .dt.tz_localize("Asia/Seoul", nonexistent="NaT", ambiguous="NaT")
                .dt.tz_convert("UTC")
            )
    return df


def flag_missing_key(df: pd.DataFrame, key_columns: list) -> pd.DataFrame:
    """
    기본키 결측 플래그 함수
    (조인과 중복 제거에 필요한 핵심 키가 없을 경우 MISSING_KEY 표시)
    """
    df = df.copy()

    available_keys = [col for col in key_columns if col in df.columns]

    if not available_keys:
        df["quality_flag"] = "MISSING_KEY"
        return df

    missing_mask = df[available_keys].isna().any(axis=1)
    df.loc[missing_mask, "quality_flag"] = "MISSING_KEY"

    return df


def flag_ulsan_bbox(df: pd.DataFrame) -> pd.DataFrame:
    """
    울산항 좌표 범위 검증 함수
    (울산항 관제 범위 밖 좌표는 OUT_OF_ULSAN_BBOX로 표시)
    """
    df = df.copy()

    if "latitude" not in df.columns or "longitude" not in df.columns:
        return df

    missing_coord = df["latitude"].isna() | df["longitude"].isna()
    zero_coord = (df["latitude"] == 0) | (df["longitude"] == 0)

    outside_bbox = ~(
        df["latitude"].between(ULSAN_LAT_MIN, ULSAN_LAT_MAX)
        & df["longitude"].between(ULSAN_LON_MIN, ULSAN_LON_MAX)
    )

    df.loc[missing_coord | zero_coord, "quality_flag"] = "MISSING_COORDINATE"
    df.loc[~missing_coord & ~zero_coord & outside_bbox, "quality_flag"] = "OUT_OF_ULSAN_BBOX"

    return df


def create_port_call_id(df: pd.DataFrame) -> pd.DataFrame:
    """
    입항 건 ID 생성 함수
    (callsgn + ptent_yr + voyage_no를 합쳐 선박의 특정 입항 1건을 구분)
    voyage_no가 없으면 arrival_at_utc 날짜를 이용해 임시 ID 생성
    """
    df = df.copy()

    if "callsgn" not in df.columns:
        df["callsgn"] = np.nan

    if "ptent_yr" not in df.columns:
        df["ptent_yr"] = np.nan

    if "voyage_no" not in df.columns:
        df["voyage_no"] = np.nan

    if "arrival_at_utc" in df.columns:
        arrival_date = (
            pd.to_datetime(df["arrival_at_utc"], errors="coerce", utc=True)
            .dt.strftime("%Y%m%d")
        )
    else:
        arrival_date = pd.Series(["UNKNOWN_DATE"] * len(df), index=df.index)

    df["voyage_no_filled"] = df["voyage_no"]
    missing_voyage = df["voyage_no_filled"].isna()

    df.loc[missing_voyage, "voyage_no_filled"] = arrival_date[missing_voyage]

    df["port_call_id"] = (
        df["callsgn"].astype(str).str.strip()
        + "_"
        + df["ptent_yr"].astype(str).str.strip()
        + "_"
        + df["voyage_no_filled"].astype(str).str.strip()
    )

    df.loc[missing_voyage, "quality_flag"] = "MISSING_VOYAGE_NO"

    return df


def validate_speed_course(df: pd.DataFrame) -> pd.DataFrame:
    """
    속도·침로 검증 함수
    (sog는 음수일 수 없고, cog는 0~360도 범위여야 함)
    """
    df = df.copy()

    if "sog" in df.columns:
        invalid_speed = (df["sog"] < 0) | (df["sog"] > 30)
        df.loc[invalid_speed & df["sog"].notna(), "quality_flag"] = "INVALID_SPEED"

    if "cog" in df.columns:
        invalid_course = ~df["cog"].between(0, 360)
        df.loc[invalid_course & df["cog"].notna(), "quality_flag"] = "INVALID_COURSE"

    return df


def validate_date_order(
    df: pd.DataFrame,
    start_col: str,
    end_col: str,
) -> pd.DataFrame:
    """
    날짜 순서 검증 함수
    (출항일시가 입항일시보다 빠르거나, 완료일시가 시작일시보다 빠른 경우 오류 표시)
    """
    df = df.copy()

    if start_col in df.columns and end_col in df.columns:
        invalid_order = (
            df[start_col].notna()
            & df[end_col].notna()
            & (df[end_col] < df[start_col])
        )
        df.loc[invalid_order, "quality_flag"] = "INVALID_DATE_ORDER"

    return df


def save_staging_csv(df: pd.DataFrame, output_path: str) -> None:
    """
    staging CSV 저장 함수
    (전처리 결과를 UTF-8-SIG 형식으로 저장해 한글 깨짐을 방지)
    """
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
