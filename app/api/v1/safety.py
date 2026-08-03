from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import SafetyAssessmentRequest, SafetyAssessmentResult
from app.agents.safety.service import assess_safety
from app.core.deps import get_llm_client, get_session
from app.core.exceptions import LLMGenerationError, MsdsNotFoundError, MsdsUpstreamError
from app.llm.base import LLMClient
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/safety", tags=["safety"])

_DESCRIPTION = """
대상 화물과 인접 화물의 혼재 위험을 판정합니다. MSDS 텍스트 기반 혼재금지와 IMDG 공인
격리표를 함께 봅니다.

> 선석 배정 화면이라면 `POST /orchestrator/assess`를 쓰세요. 이 API는 화물 조합이 이미
> 정해진 상태의 단독 판정용입니다. 기상은 다루지 않습니다.

- **`risk_level`**: `안전` < `주의` < `위험` < `배정불가`. **`배정불가`면 접안을 막으세요.**
- **`rule_engine_floor`**는 그래프 탐색으로 계산한 결정적 하한이고 LLM은 이보다 낮출 수
  없습니다. `risk_level`과 함께 노출하면 "규칙이 정한 최소 등급"과 "AI가 추가로 본
  위험"을 구분할 수 있습니다.
- **`imdg_conflicts`가 법적 근거**(IMDG Code Ch.7.2)라 `conflicts`(MSDS 텍스트 추출)보다
  우선 표시하세요. `segregation_code`는 1~4이며 클수록 강한 격리를 요구합니다.
- `msds_sections_used`는 판정 근거로 쓴 MSDS 섹션 키입니다.

배경: `01_안전관제_에이전트_설계문서.md`
"""

_RESPONSES: dict = {
    404: {"description": "대상 또는 인접 화물의 MSDS를 찾을 수 없음 (KOSHA 미등재)"},
    502: {"description": "KOSHA MSDS API 또는 LLM 호출 실패. 재시도 가능"},
}


@router.post(
    "/assess",
    response_model=SafetyAssessmentResult,
    summary="화물 혼재 위험 판정",
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
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
