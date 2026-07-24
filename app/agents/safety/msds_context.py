"""화물 식별 + MSDS 요약 추출.

Neo4j 그래프는 화물 간 관계(혼재금지 등) 탐색에만 쓰고, LLM 프롬프트에 넣을 실제
유해성/응급조치/보호구 문구는 PostgreSQL의 msds_chemical.msds_payload(JSONB 원문)에서
가져온다. 체크리스트를 "제공된 문구에 근거해서만" 작성하게 해 환각을 억제하려는
목적이므로, 여기서 뽑는 텍스트가 곧 LLM의 근거 자료가 된다.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import MsdsNotFoundError
from app.models import MsdsChemical
from app.services.msds_service import get_or_fetch_msds

from .schemas import CargoRef

# 안전관제 판단에 실제로 쓰는 MSDS 섹션만 선별 (16개 전체를 프롬프트에 넣으면 노이즈만 커짐)
RELEVANT_SECTIONS: dict[str, str] = {
    "detail02": "유해성·위험성",
    "detail04": "응급조치요령",
    "detail07": "취급 및 저장방법",
    "detail08": "노출방지 및 개인보호구",
    "detail10": "안정성 및 반응성",
}

_NULL_VALUES = frozenset({"자료없음", "해당없음", "-", "", "N/A", "없음"})


async def get_by_chem_id(db: AsyncSession, chem_id: str) -> MsdsChemical:
    row = await db.scalar(select(MsdsChemical).where(MsdsChemical.chem_id == chem_id))
    if row is None:
        raise MsdsNotFoundError(chem_id)
    return row


async def resolve_cargo(db: AsyncSession, ref: CargoRef) -> MsdsChemical:
    """CargoRef(chem_id 또는 cas_no)를 실제 MsdsChemical 레코드로 해석한다.

    cas_no가 있으면 기존 lazy-fetch 경로(get_or_fetch_msds)를 그대로 타서 DB에
    없으면 KOSHA API로 즉시 채운다. chem_id만 있으면 DB에 이미 있는 레코드만
    조회한다(신규 화물은 CAS 기준으로 들어오는 게 일반적이므로).
    """
    if ref.cas_no:
        return await get_or_fetch_msds(db, ref.cas_no)
    assert ref.chem_id is not None  # CargoRef validator가 보장
    return await get_by_chem_id(db, ref.chem_id)


def summarize_hazard_sections(msds_payload: dict) -> dict[str, list[str]]:
    """관련 섹션에서 itemDetail 텍스트만 뽑아 { 섹션키: [문장, ...] } 형태로 반환."""
    summary: dict[str, list[str]] = {}
    for section_key in RELEVANT_SECTIONS:
        section = msds_payload.get(section_key) or {}
        data = section.get("data") if isinstance(section, dict) else None
        if not isinstance(data, list):
            continue

        lines: list[str] = []
        for item in data:
            detail = (item.get("itemDetail") or "").strip()
            if detail and detail not in _NULL_VALUES:
                lines.append(detail)
        if lines:
            summary[section_key] = lines

    return summary
