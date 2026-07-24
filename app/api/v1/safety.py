from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import SafetyAssessmentRequest, SafetyAssessmentResult
from app.agents.safety.service import assess_safety
from app.core.deps import get_llm_client, get_session
from app.core.exceptions import LLMGenerationError, MsdsNotFoundError, MsdsUpstreamError
from app.llm.base import LLMClient
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/safety", tags=["safety"])


@router.post("/assess", response_model=SafetyAssessmentResult)
async def assess(
    request: SafetyAssessmentRequest,
    db: AsyncSession = Depends(get_session),
    llm_client: LLMClient = Depends(get_llm_client),
) -> SafetyAssessmentResult:
    try:
        return await assess_safety(db, neo4j_client.driver, llm_client, request)
    except MsdsNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"MSDS not found for identifier: {e.identifier}")
    except MsdsUpstreamError as e:
        raise HTTPException(status_code=502, detail=f"KOSHA MSDS API 호출 실패: {e.reason}")
    except LLMGenerationError as e:
        raise HTTPException(status_code=502, detail=f"LLM 판단 생성 실패 ({e.provider}): {e.reason}")
