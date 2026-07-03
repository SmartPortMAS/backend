from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.core.exceptions import MsdsNotFoundError, MsdsUpstreamError
from app.schemas.msds import MsdsChemicalResponse
from app.services.msds_service import get_or_fetch_msds

router = APIRouter(prefix="/msds", tags=["msds"])


@router.get("/{cas_no}", response_model=MsdsChemicalResponse)
async def get_msds(cas_no: str, db: AsyncSession = Depends(get_session)) -> MsdsChemicalResponse:
    try:
        chemical = await get_or_fetch_msds(db, cas_no)
    except MsdsNotFoundError:
        raise HTTPException(status_code=404, detail=f"MSDS not found for CAS No. {cas_no}")
    except MsdsUpstreamError as e:
        raise HTTPException(status_code=502, detail=f"KOSHA MSDS API 호출 실패: {e.reason}")
    return MsdsChemicalResponse.model_validate(chemical)
