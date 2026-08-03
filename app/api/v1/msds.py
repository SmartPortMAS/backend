from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.core.exceptions import MsdsNotFoundError, MsdsUpstreamError
from app.schemas.msds import MsdsChemicalResponse
from app.services.msds_service import get_or_fetch_msds

router = APIRouter(prefix="/msds", tags=["msds"])


_DESCRIPTION = """
CAS번호로 MSDS 원문을 조회합니다. **16개 섹션 전체가 `msds_payload`(JSONB)에 그대로
들어 있어 응답이 큽니다** — 화면에 특정 값 하나만 필요하다면 `POST /rag/query`나
선석 관련 API의 요약 필드를 쓰는 편이 낫습니다.

### lazy-fetch 동작

DB에 없는 CAS번호면 **KOSHA MSDS API를 실시간으로 호출해 가져온 뒤 저장**합니다.
따라서 첫 호출은 느릴 수 있고(수 초), 두 번째부터는 DB에서 바로 나옵니다.

> ⚠️ **이렇게 새로 들어온 화물은 지식그래프에 없습니다.** 혼재금지·IMDG 판정을 할 수
> 없는 상태이며, `/rag/query`는 이를 "판정 불가"로 표시합니다. 그래프에 반영하려면
> data-pipeline의 `msds_neo4j_loader` · `imdg_segregation_loader` ·
> `cargo_category_loader`를 함께 실행해야 합니다.

`GET /chatbot/chemicals`가 반환하는 목록이 "실제로 판정까지 가능한 화물"입니다.
"""


@router.get(
    "/{cas_no}",
    response_model=MsdsChemicalResponse,
    summary="CAS번호로 MSDS 원문 조회",
    description=_DESCRIPTION,
    responses={
        404: {"description": "해당 CAS번호의 MSDS가 DB에도 KOSHA에도 없음"},
        502: {"description": "KOSHA MSDS API 호출 실패. 재시도 가능"},
    },
)
async def get_msds(cas_no: str, db: AsyncSession = Depends(get_session)) -> MsdsChemicalResponse:
    try:
        chemical = await get_or_fetch_msds(db, cas_no)
    except MsdsNotFoundError:
        raise HTTPException(status_code=404, detail=f"MSDS not found for CAS No. {cas_no}")
    except MsdsUpstreamError as e:
        raise HTTPException(status_code=502, detail=f"KOSHA MSDS API 호출 실패: {e.reason}")
    return MsdsChemicalResponse.model_validate(chemical)
