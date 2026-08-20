from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.scheduling.schemas import SchedulingRequest, SchedulingResult
from app.agents.scheduling.service import find_berth_candidates
from app.core.deps import get_session
from app.core.exceptions import CargoCategoryUnknownError, MsdsNotFoundError, MsdsUpstreamError
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/scheduling", tags=["scheduling"])

_DESCRIPTION = """
화물 카테고리와 흘수 조건을 만족하는 선석 후보 상위 3순위를 반환합니다. LLM을 쓰지
않는 결정적 판단입니다.

> 선석 배정 화면이라면 `POST /orchestrator/assess`를 쓰세요. 이 API는 후보 목록만
> 필요할 때 씁니다.

정렬 키: `온산 스코프` → `여유 선석` → `흘수 여유 큰 순`. 온산 우선은 하드 필터가
아니라 정렬이라, 온산 후보가 3개 미만인 화물(원유 등)에서도 후보가 사라지지 않습니다.

- **`berth_group`이 `null`인 것을 "온산 밖"으로 읽지 마세요.** 그 필드는 "기상 임계값
  자료가 있는가"입니다. 온산 여부는 **`onsan_scope`**로 판단하세요.
- `occupancy_status`는 우리 시스템 배정 기록(berth_assignment)만 기준입니다(2026-08-19
  결정 — VTS 실측 upa_port_call은 안 봄). `점유`면 `conflicting_port_calls`에 겹치는
  배정 건이 옵니다.
- **`adjacent_cargos`는 `POST /safety/assess` 요청 바디로 변환 없이 그대로 넘길 수
  있습니다.**
- `total_eligible_count`는 조건을 만족한 전체 수입니다(반환은 상위 3개).

배경: `02_스케줄링_에이전트_설계문서.md`
"""

_RESPONSES: dict = {
    404: {"description": "요청한 화물의 MSDS를 찾을 수 없음 (KOSHA 미등재)"},
    422: {"description": "요청 형식 오류, 또는 화물에 선석 카테고리(cargo_category)가 "
                         "지정되지 않아 후보를 탐색할 수 없음"},
    502: {"description": "KOSHA MSDS API 호출 실패. 재시도 가능"},
}


@router.post(
    "/candidates",
    response_model=SchedulingResult,
    summary="선석 후보 상위 3순위 조회",
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
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
