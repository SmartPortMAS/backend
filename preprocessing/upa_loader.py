# -*- coding: utf-8 -*-
"""
울산항만공사(UPA) staging → PostgreSQL 적재기 (담당: 함현우)

전처리 결과(data/staging/*.csv)를 PostgreSQL(RDS 등)에 적재한다.
- 인프라 독립적: APScheduler/Lambda/수동 실행 어디서든 함수 호출만 하면 됨
- 멱등(idempotent) 적재: 같은 데이터를 다시 넣어도 중복이 쌓이지 않음
  (메타 컬럼 collected_at_utc 를 제외한 모든 컬럼으로 record_uid 해시를 만들고,
   record_uid 기준 UPSERT → 동일 행은 갱신, 새 행만 INSERT)

준비:
  pip install sqlalchemy psycopg2-binary pandas
  환경변수로 접속정보 지정 (둘 중 하나):
    1) DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/dbname
    2) PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE

실행:
  python -m backend.preprocessing.upa_loader
"""
import os
import glob
import hashlib

import pandas as pd
from sqlalchemy import create_engine, inspect, text


STAGING_DIR = "data/staging"

# staging 파일명 -> DB 테이블명
TABLE_MAP = {
    "upa_vessel_position_stg.csv": "upa_vessel_position",
    "upa_port_call_stg.csv": "upa_port_call",
    "upa_cargo_manifest_stg.csv": "upa_cargo_manifest",
    "upa_unload_record_stg.csv": "upa_unload_record",
    "upa_berth_facility_stg.csv": "upa_berth_facility",
    "upa_anchorage_stg.csv": "upa_anchorage",
}

# 해시(record_uid)에서 제외할 컬럼 (실행마다 바뀌는 값)
VOLATILE_COLS = ["collected_at_utc"]


def get_engine():
    """환경변수에서 PostgreSQL 접속 엔진 생성."""
    url = os.getenv("DATABASE_URL")
    if not url:
        host = os.getenv("PGHOST", "localhost")
        port = os.getenv("PGPORT", "5432")
        user = os.getenv("PGUSER", "postgres")
        pw = os.getenv("PGPASSWORD", "")
        db = os.getenv("PGDATABASE", "smartport")
        url = f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{db}"
    return create_engine(url)


def add_record_uid(df: pd.DataFrame) -> pd.DataFrame:
    """변동 컬럼을 제외한 전체 컬럼으로 행 고유 해시(record_uid)를 만든다."""
    df = df.copy()
    cols = [c for c in df.columns if c not in VOLATILE_COLS and c != "record_uid"]
    joined = df[cols].astype(str).fillna("").agg("|".join, axis=1)
    df["record_uid"] = joined.map(lambda x: hashlib.md5(x.encode("utf-8")).hexdigest())
    # 같은 배치 안의 완전 중복 제거
    df = df.drop_duplicates(subset=["record_uid"], keep="last").reset_index(drop=True)
    return df


def _ensure_table(engine, table: str, df: pd.DataFrame) -> None:
    """테이블이 없으면 DataFrame 스키마로 생성하고 record_uid 유니크 인덱스를 건다."""
    insp = inspect(engine)
    if not insp.has_table(table):
        df.head(0).to_sql(table, engine, index=False)
        with engine.begin() as conn:
            conn.execute(text(
                f'CREATE UNIQUE INDEX IF NOT EXISTS "{table}_uid_idx" '
                f'ON "{table}" (record_uid)'
            ))
        print(f"  - 테이블 생성: {table}")


def upsert_dataframe(engine, table: str, df: pd.DataFrame) -> int:
    """임시 테이블 경유 UPSERT (ON CONFLICT record_uid)."""
    if df.empty:
        return 0
    _ensure_table(engine, table, df)

    cols = list(df.columns)
    tmp = f"_tmp_{table}"
    df.to_sql(tmp, engine, if_exists="replace", index=False)

    collist = ", ".join(f'"{c}"' for c in cols)
    updates = ", ".join(f'"{c}"=EXCLUDED."{c}"' for c in cols if c != "record_uid")
    sql = (
        f'INSERT INTO "{table}" ({collist}) '
        f'SELECT {collist} FROM "{tmp}" '
        f'ON CONFLICT (record_uid) DO UPDATE SET {updates};'
    )
    with engine.begin() as conn:
        conn.execute(text(sql))
        conn.execute(text(f'DROP TABLE IF EXISTS "{tmp}";'))
    return len(df)


def load_csv(engine, csv_path: str, table: str) -> int:
    df = pd.read_csv(csv_path)
    if df.empty:
        print(f"[SKIP] {csv_path} 비어 있음")
        return 0
    df = add_record_uid(df)
    n = upsert_dataframe(engine, table, df)
    print(f"[OK] {os.path.basename(csv_path)} -> {table} ({n} rows upsert)")
    return n


def load_all(staging_dir: str = STAGING_DIR) -> None:
    """staging 폴더의 모든 UPA staging CSV 를 PostgreSQL 에 적재."""
    engine = get_engine()
    total = 0
    for csv_path in sorted(glob.glob(os.path.join(staging_dir, "*.csv"))):
        fname = os.path.basename(csv_path)
        table = TABLE_MAP.get(fname)
        if not table:
            continue  # UPA staging 이 아닌 파일은 건너뜀
        total += load_csv(engine, csv_path, table)
    print(f"[DONE] 총 {total} rows 적재 완료")


if __name__ == "__main__":
    load_all()
