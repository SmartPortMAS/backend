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
**선석 배정 화면에서 쓸 단일 엔드포인트입니다.** 기상·스케줄링·안전관제 세 에이전트를
순서대로 호출해 "이 배를 지금 접안시켜도 되는가"에 대한 최종 결론 하나를 냅니다.
개별 에이전트 API를 각각 호출해 프론트에서 합치지 마세요 — 판단 순서와 조기 종료
조건이 여기 들어 있습니다.

### 판정 순서

1. **기상** — 전역 기상으로 1차 판정. `NORMAL`이 아니면 즉시 `기상불가_중단권고`로 종료
2. **선석 후보** — 화물 카테고리·수심으로 후보 탐색. 없으면 `적합선석없음`
3. **선석별 기상 재판정** — 확정된 선석의 `berth_group` 임계값으로 다시 판정.
   같은 기상이라도 선석마다 결과가 갈립니다(정일 17m/s vs OTK 14m/s)
4. **안전관제** — 인접 화물과의 혼재 판정. `배정불가`면 차순위 후보로 재탐색

### `overall_decision` 읽는 법

| 값 | 의미 | 화면 |
|---|---|---|
| `승인가능` | 접안 가능 | `selected_berth` 표시 |
| `기상불가_중단권고` | 기상 때문에 중단 | `weather_assessment.reasons` 표시 |
| `적합선석없음` | 화물·흘수 조건을 만족하는 선석이 없음 | — |
| `전후보배정불가` | 후보는 있으나 전부 혼재 위험 | `rejected_candidates` 표시 |
| `정박지대기` | 전용 선석 점유 + 대체 없음 → 톤수에 맞는 정박지 대기 | `anchorage_assignment` 표시 |

**`승인가능` 외 네 상태는 모두 "지금 하역하면 안 되는" 상태**입니다. 관제사가 원인을
한눈에 구분하도록 분리한 것이니 화면에서도 구분해 주세요.

### 함께 보내는 근거

- `assignment_trace` — 전용 → 대체 → 정박지 판단 경로를 사람이 읽는 문장으로. 왜 이
  선석이 나왔는지 설명할 때 그대로 노출하면 됩니다
- `rejected_candidates` — 배정불가로 탈락한 후보 이력
- `safety_assessment` / `weather_assessment` — 각 에이전트의 판정 원문
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
