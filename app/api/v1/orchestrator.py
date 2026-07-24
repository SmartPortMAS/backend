from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.schemas import OrchestratorRequest, OrchestratorResult
from app.agents.orchestrator.service import orchestrate
from app.core.deps import get_llm_client, get_session
from app.core.exceptions import (
    CargoCategoryUnknownError,
    LLMGenerationError,
    MsdsNotFoundError,
    MsdsUpstreamError,
)
from app.llm.base import LLMClient
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


@router.post("/assess", response_model=OrchestratorResult)
async def assess(
    request: OrchestratorRequest,
    db: AsyncSession = Depends(get_session),
    llm_client: LLMClient = Depends(get_llm_client),
) -> OrchestratorResult:
    try:
        return await orchestrate(db, neo4j_client.driver, llm_client, request)
    except MsdsNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"MSDS not found for identifier: {e.identifier}")
    except MsdsUpstreamError as e:
        raise HTTPException(status_code=502, detail=f"KOSHA MSDS API 호출 실패: {e.reason}")
    except CargoCategoryUnknownError as e:
        raise HTTPException(
            status_code=422,
            detail=f"화물({e.chem_id})에 선석 카테고리(cargo_category)가 지정되어 있지 않습니다.",
        )
    except LLMGenerationError as e:
        raise HTTPException(status_code=502, detail=f"LLM 판단 생성 실패 ({e.provider}): {e.reason}")
