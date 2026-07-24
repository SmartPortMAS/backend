from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.scheduling.schemas import SchedulingRequest, SchedulingResult
from app.agents.scheduling.service import find_berth_candidates
from app.core.deps import get_session
from app.core.exceptions import CargoCategoryUnknownError, MsdsNotFoundError, MsdsUpstreamError
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/scheduling", tags=["scheduling"])


@router.post("/candidates", response_model=SchedulingResult)
async def candidates(
    request: SchedulingRequest,
    db: AsyncSession = Depends(get_session),
) -> SchedulingResult:
    try:
        return await find_berth_candidates(db, neo4j_client.driver, request)
    except MsdsNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"MSDS not found for identifier: {e.identifier}")
    except MsdsUpstreamError as e:
        raise HTTPException(status_code=502, detail=f"KOSHA MSDS API 호출 실패: {e.reason}")
    except CargoCategoryUnknownError as e:
        raise HTTPException(
            status_code=422,
            detail=f"화물({e.chem_id})에 선석 카테고리(cargo_category)가 지정되어 있지 않습니다.",
        )
