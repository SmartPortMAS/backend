"""MSDS 조회 서비스 — DB 우선 조회, miss 시 KOSHA API 호출 후 적재.

data-pipeline/data_pipeline/loaders/msds_pg_loader.py 의 upsert 스키마(msds_chemical,
ON CONFLICT (chem_id) DO UPDATE)를 그대로 따른다. 다만 스키마 소유권(테이블/인덱스 생성)은
backend(Alembic)에 있고, data-pipeline은 insert만 수행한다.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import MsdsNotFoundError
from app.models import MsdsChemical
from app.services.kosha_client import DETAIL_ENDPOINTS, fetch_full_msds_record

SECTION_KEYS = [key for key, _, _ in DETAIL_ENDPOINTS]


def _build_row(record: dict) -> dict:
    list_info: dict = record.get("list_info") or {}

    chem_id = (record.get("_chem_id") or list_info.get("chemId") or "").strip()
    cas_no = record.get("_query_cas_no") or list_info.get("casNo") or None
    un_no = list_info.get("unNo") or None
    name_ko = list_info.get("chemNameKor") or None
    name_en = list_info.get("chemEngNm") or None

    payload: dict = {"list_info": list_info}
    for key in SECTION_KEYS:
        section = record.get(key)
        if section is not None:
            payload[key] = section

    quality_flag = "OK" if (chem_id and name_ko) else "MISSING_KEY"

    return {
        "chem_id": chem_id,
        "cas_no": cas_no,
        "un_no": un_no,
        "name_ko": name_ko,
        "name_en": name_en,
        "source_system": "KOSHA_MSDS_API",
        "source_table": "getChemDetail",
        "collected_at_utc": datetime.now(tz=timezone.utc),
        "quality_flag": quality_flag,
        "is_synthetic": False,
        "msds_payload": payload,
    }


async def _upsert(db: AsyncSession, row: dict) -> MsdsChemical:
    stmt = pg_insert(MsdsChemical).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[MsdsChemical.chem_id],
        set_={
            "cas_no": stmt.excluded.cas_no,
            "un_no": stmt.excluded.un_no,
            "name_ko": stmt.excluded.name_ko,
            "name_en": stmt.excluded.name_en,
            "source_system": stmt.excluded.source_system,
            "source_table": stmt.excluded.source_table,
            "collected_at_utc": stmt.excluded.collected_at_utc,
            "quality_flag": stmt.excluded.quality_flag,
            "msds_payload": stmt.excluded.msds_payload,
        },
    ).returning(MsdsChemical)

    result = await db.execute(stmt)
    await db.commit()
    return result.scalar_one()


async def get_or_fetch_msds(db: AsyncSession, cas_no: str) -> MsdsChemical:
    """CAS번호로 DB를 조회하고, 없으면 KOSHA API에서 가져와 적재한 뒤 반환한다."""
    existing = await db.scalar(select(MsdsChemical).where(MsdsChemical.cas_no == cas_no))
    if existing is not None:
        return existing

    record = await fetch_full_msds_record(cas_no)
    if record is None:
        raise MsdsNotFoundError(cas_no)

    row = _build_row(record)
    if not row["chem_id"]:
        raise MsdsNotFoundError(cas_no)

    return await _upsert(db, row)
