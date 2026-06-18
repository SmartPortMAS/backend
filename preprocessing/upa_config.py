# -*- coding: utf-8 -*-
"""
울산항만공사(UPA) Open API 전처리 설정

각 API별로 다음을 정의한다.
- column_map   : 원본 컬럼명 -> 팀 공통 표준 컬럼명 (snake_case)
- numeric_cols : to_numeric_safe 대상 (표준 컬럼명 기준)
- utc_cols     : KST -> UTC 변환 대상 시간 컬럼 (표준 컬럼명 기준)
- key_cols     : flag_missing_key 대상 기본키 (표준 컬럼명 기준)
- source_table : 원본 API명 (메타컬럼 source_table 값)
- stg_name     : staging 파일명 (공통 제출 파일 규칙)

source_system 은 모든 UPA API 공통으로 "UPA" 를 사용한다.
표준 컬럼명은 공통 기준 문서 3-2(컬럼명 기준)를 따른다.
"""

SOURCE_SYSTEM = "UPA"

# ---------------------------------------------------------------------------
# 1. 항만 내 선박 운항정보 (getVtsBaseVslNvgtInfo) -> 입항 건(port call)
# ---------------------------------------------------------------------------
VSL_NVGT_INFO = {
    "source_table": "VtsBaseVslNvgtInfo",
    "stg_name": "upa_port_call_stg.csv",
    "column_map": {
        "callsgn": "callsgn",
        "ptentVtsYr": "ptent_vts_yr",   # 입항운항관제연도
        "vyg": "voyage_no",
        "comCnt": "comm_count",
        "vslKornNm": "vessel_name",
        "vslEngNm": "vessel_name_en",
        "ptentDt": "arrival_at_utc",     # 입항일시
        "dfptDt": "departure_at_utc",    # 출항일시
        "ioprtVtsType": "io_vts_type_code",
        "ioprtVtsNm": "io_vts_name",
        "fcltSpecCd": "facility_spec_code",
        "fcltSpecSubCd": "facility_spec_sub_code",
        "fcltKornNm": "facility_name",
        "updtDt": "updated_at_utc",
        "jobDt": "job_at_utc",
    },
    "numeric_cols": ["comm_count"],
    "utc_cols": [
        "arrival_at_utc",
        "departure_at_utc",
        "updated_at_utc",
        "job_at_utc",
    ],
    # port_call_id 는 callsgn + ptent_yr + voyage_no. 이 API는 ptentVtsYr만 있으므로
    # 파이프라인에서 ptent_yr = ptent_vts_yr 로 채운다.
    "key_cols": ["callsgn", "ptent_vts_yr"],
}

# ---------------------------------------------------------------------------
# 2. 항내 선박위치정보 (getVslPstnInfo) -> 선박 위치
# ---------------------------------------------------------------------------
VSL_PSTN_INFO = {
    "source_table": "VslPstnInfo",
    "stg_name": "upa_vessel_position_stg.csv",
    "column_map": {
        "callsgn": "callsgn",
        "ptentYr": "ptent_yr",
        "vyg": "voyage_no",
        "mmsiNo": "mmsi",
        "imoNo": "imo_no",
        "vslNm": "vessel_name",
        "lot": "longitude",
        "lat": "latitude",
        "sog": "sog",
        "cog": "cog",
        "rot": "rot",
        "hdgAng": "heading",
        "drft": "draught",
        "nvgtStts": "nav_status_code",
        "updtTm": "received_at_utc",
    },
    "numeric_cols": [
        "mmsi", "imo_no", "longitude", "latitude",
        "sog", "cog", "rot", "heading", "draught",
    ],
    "utc_cols": ["received_at_utc"],
    "key_cols": ["callsgn", "received_at_utc"],
}

# ---------------------------------------------------------------------------
# 3-1. 울산항 통합화물 정보 (getIntgCagInfo) -> 화물 manifest
# ---------------------------------------------------------------------------
INTG_CAG_INFO = {
    "source_table": "IntgCagInfo",
    "stg_name": "upa_cargo_manifest_stg.csv",
    "column_map": {
        "prtCd": "port_code",
        "ptentYr": "ptent_yr",
        "vyg": "voyage_no",
        "callsgn": "callsgn",
        "vslKornNm": "vessel_name",
        "vslTypeNm": "vessel_type_name",
        "vslNtnltyCd": "vessel_nationality_code",
        "vslNtnltyNm": "vessel_nationality_name",
        "mrnNo": "mrn_no",
        "blNo": "bl_no",
        "msnNo": "master_bl_no",
        "ioSeCd": "io_se_code",
        "ioSeNm": "io_se_name",
        "fcltKornNm": "facility_name",
        "cagSeNm": "cargo_se_name",
        "cagItemNm": "cargo_name_raw",       # 화물명 (MSDS 매핑 입력)
        "dgUnCd": "dg_un_no",                # 위험물 UN 번호 (MSDS 조인키)
        "pkgTypeMpngNm": "package_type_name",
        "unloadMthdNm": "unload_method_name",
        "volTonUnitNm": "vol_ton_unit_name",
        "volTon": "vol_ton",
        "wghtTon": "weight_ton",
        "volSz": "vol_size",
        "wghtSz": "weight_size",
        "bulkVolSz": "bulk_vol_size",
        "bulkWghtSz": "bulk_weight_size",
        "conCnt": "container_count",
        "podNm": "pod_name",                 # 양하항
        "polNm": "pol_name",                 # 적하항
        "lastDestPrtNm": "last_dest_port_name",
        "ptentDt": "arrival_at_utc",
        "csclPrgrsSttsNm": "customs_progress_status_name",
    },
    "numeric_cols": [
        "vol_ton", "weight_ton", "vol_size", "weight_size",
        "bulk_vol_size", "bulk_weight_size", "container_count",
    ],
    "utc_cols": ["arrival_at_utc"],
    "key_cols": ["callsgn", "ptent_yr", "voyage_no"],
}

# ---------------------------------------------------------------------------
# 3-2. 울산항 내항화물 정보 (getInprtCagDclrInfo) -> 화물 manifest (내항)
# ---------------------------------------------------------------------------
INPRT_CAG_DCLR_INFO = {
    "source_table": "InprtCagDclrInfo",
    "stg_name": "upa_cargo_manifest_stg.csv",  # 통합화물과 동일 staging 으로 concat
    "column_map": {
        "prtCd": "port_code",
        "ptentYr": "ptent_yr",
        "vyg": "voyage_no",
        "callsgn": "callsgn",
        "vslKornNm": "vessel_name",
        "vslTypeNm": "vessel_type_name",
        "ioSeNm": "io_se_name",
        "fcltKornNm": "facility_name",
        "pkgTypeMpngNm": "package_type_name",
        "unloadMthdNm": "unload_method_name",
        "volTonUnitNm": "vol_ton_unit_name",
        "volTon": "vol_ton",
        "wghtTon": "weight_ton",
        "ldudPrtNm": "ldud_port_name",       # 양적하 항구명
        "ptentDt": "arrival_at_utc",
    },
    "numeric_cols": ["vol_ton", "weight_ton"],
    "utc_cols": ["arrival_at_utc"],
    "key_cols": ["callsgn", "ptent_yr", "voyage_no"],
}

# ---------------------------------------------------------------------------
# 4. 선박 하역정보 (getUnloadRcdInfo) -> 하역 기록
# ---------------------------------------------------------------------------
UNLOAD_RCD_INFO = {
    "source_table": "UnloadRcdInfo",
    "stg_name": "upa_unload_record_stg.csv",
    "column_map": {
        "unloadRcdId": "unload_record_id",
        "unloadPrtNm": "unload_port_name",
        "unloadRcdDate": "unload_record_date",
        "vslNm": "vessel_name",
        "loadPrtNm": "load_port_name",
        "prdctTypeCn": "product_type",
        "blCagQtyCn": "bl_cargo_qty",
        "norSbmsnDate": "nor_submission_at_utc",
        "norAprvDate": "nor_approval_at_utc",
        "unloadBgngDate": "unload_begin_at_utc",
        "unloadCmptnDate": "unload_complete_at_utc",
        "loadBgngDate": "load_begin_at_utc",
        "loadCmptnDate": "load_complete_at_utc",
        "jobRcdInfoCn": "job_record_info",
        "rmrkInfoCn": "remark",
        "regDt": "registered_at_utc",
        "blNoCn": "bl_no",
    },
    "numeric_cols": [],
    "utc_cols": [
        "nor_submission_at_utc",
        "nor_approval_at_utc",
        "unload_begin_at_utc",
        "unload_complete_at_utc",
        "load_begin_at_utc",
        "load_complete_at_utc",
        "registered_at_utc",
    ],
    # 하역정보는 callsgn/voyage_no 가 없어 vessel_name + bl_no 로만 식별 가능
    "key_cols": ["unload_record_id"],
    # 날짜 순서 검증 쌍 (start, end)
    "date_order_pairs": [
        ("nor_submission_at_utc", "nor_approval_at_utc"),
        ("unload_begin_at_utc", "unload_complete_at_utc"),
        ("load_begin_at_utc", "load_complete_at_utc"),
    ],
}

# ---------------------------------------------------------------------------
# 5-1. 항만시설(부두) 현황 상세정보 (getGisBaseHrbrFcltDtlInfo) -> 부두
# ---------------------------------------------------------------------------
HRBR_FCLT_INFO = {
    "source_table": "GisBaseHrbrFcltDtlInfo",
    "stg_name": "upa_berth_facility_stg.csv",
    "column_map": {
        "prtNm": "port_name",
        "whrfNm": "wharf_name",
        "len": "length_m",
        "dow": "depth_m",                    # 수심
        "brthdCapVl": "berth_capacity",
        "brthdVslCntVl": "berth_vessel_count",
        "unloadCapVl": "unload_capacity",
        "hndlCrgNm": "handling_cargo_name",
        "whrfSeNm": "wharf_se_name",
        "lat": "latitude",
        "lot": "longitude",
        "ptopNm": "port_operator_name",
    },
    "numeric_cols": [
        "length_m", "depth_m", "berth_capacity",
        "berth_vessel_count", "unload_capacity",
        "latitude", "longitude",
    ],
    "utc_cols": [],
    "key_cols": ["wharf_name"],
}

# ---------------------------------------------------------------------------
# 5-2. 정박지 상세정보 (getGisBaseAnchrgDtlInfo) -> 정박지
# ---------------------------------------------------------------------------
ANCHRG_INFO = {
    "source_table": "GisBaseAnchrgDtlInfo",
    "stg_name": "upa_anchorage_stg.csv",
    "column_map": {
        "anchrgNm": "anchorage_name",
        "fcltCd": "facility_code",
        "indxNo": "index_no",
        "lat": "latitude",
        "lot": "longitude",
        "rad": "radius_m",
        "rmrk": "remark",
        "anchrgType": "anchorage_type",
        "ttlYn": "title_yn",
        "sn": "seq_no",
    },
    "numeric_cols": ["latitude", "longitude", "radius_m"],
    "utc_cols": [],
    "key_cols": ["anchorage_name"],
}
