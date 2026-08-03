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

_DESCRIPTION = """
**선석 배정 화면의 주 엔드포인트.** 기상 → 선석 후보 → 선석별 기상 재판정 → 혼재 판정을
순서대로 돌려 결론 하나를 냅니다. 개별 에이전트 API를 따로 호출해 합치지 마세요 —
기상이 불가면 선석 탐색을 건너뛰고, 선석 확정 후 그 부두그룹 임계값으로 기상을 다시
판정하는 순서가 여기 들어 있습니다.

**`overall_decision`**

| 값 | 함께 볼 필드 |
|---|---|
| `승인가능` | `selected_berth` |
| `기상불가_중단권고` | `weather_assessment.reasons` |
| `적합선석없음` | — |
| `전후보배정불가` | `rejected_candidates` |
| `정박지대기` | `anchorage_assignment` |

**`승인가능` 외 네 상태는 모두 "지금 하역하면 안 되는" 상태**입니다. 관제사가 원인을
구분하도록 분리한 것이니 화면에서도 구분해 주세요.

`assignment_trace`는 전용 → 대체 → 정박지 판단 경로를 사람이 읽는 문장으로 담습니다.
왜 이 선석이 나왔는지 설명할 때 그대로 노출하면 됩니다.

배경: `04_오케스트레이터_설계문서.md`
"""

_RESPONSES: dict = {
    404: {"description": "요청한 화물의 MSDS를 찾을 수 없음 (KOSHA 미등재 CAS/chem_id)"},
    422: {"description": "요청 형식 오류, 또는 화물에 선석 카테고리(cargo_category)가 "
                         "지정되지 않아 후보 탐색 불가"},
    502: {"description": "KOSHA MSDS API 또는 LLM 호출 실패. 재시도 가능"},
}


@router.post(
    "/assess",
    response_model=OrchestratorResult,
    summary="선석 배정 종합 판정 (기상 + 스케줄링 + 안전관제)",
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
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
