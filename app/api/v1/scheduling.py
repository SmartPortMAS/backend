from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.scheduling.schemas import SchedulingRequest, SchedulingResult
from app.agents.scheduling.service import find_berth_candidates
from app.core.deps import get_session
from app.core.exceptions import CargoCategoryUnknownError, MsdsNotFoundError, MsdsUpstreamError
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/scheduling", tags=["scheduling"])

_DESCRIPTION = """
화물 카테고리와 흘수 조건을 만족하는 **선석 후보 상위 3순위**를 반환합니다.
**LLM을 쓰지 않습니다** — Neo4j 그래프 조회와 점유 확인만 하는 결정적 판단입니다.

> 선석 배정 화면이라면 이 API 대신 **`POST /orchestrator/assess`**를 쓰세요. 기상과
> 혼재 판정까지 묶어 최종 결론을 냅니다. 이 API는 후보 목록만 필요할 때 씁니다.

### 정렬 순서 — 3단 키

```
1) onsan_scope  온산 MVP 대상 선석 우선
2) 점유 여부     여유 선석 우선
3) 흘수 여유     클수록 우선(안전 마진)
```

온산 우선은 **하드 필터가 아니라 정렬**입니다. 온산 후보가 3개 미만인 화물(원유는
온산 부이 2기가 전부)에서 후보가 사라지지 않도록 하기 위함이고, 온산이 3개 이상이면
필터와 결과가 같습니다.

### 응답 읽을 때 주의

- **`berth_group`이 `null`인 것을 "온산 밖"으로 읽지 마세요.** 그 필드는 "기상 임계값
  자료가 있는가"이지 소속이 아닙니다. 온산 S-Oil 부이 2기는 `onsan_scope=true`이면서
  `berth_group=null`입니다. 온산 여부는 **`onsan_scope`**로 판단하세요
- `occupancy_status`가 `점유`면 `conflicting_port_calls`에 겹치는 입출항 기록이
  들어옵니다. `departure_at_utc`가 `null`이면 아직 출항이 기록되지 않은 것이며,
  보수적으로 점유로 판정합니다
- `adjacent_cargos`는 인접 선석의 취급 화물입니다. **`POST /safety/assess`의 요청
  바디로 변환 없이 그대로 넘길 수 있습니다**
- `total_eligible_count`는 조건을 만족한 전체 선석 수입니다(반환은 상위 3개)

`depth_m`이 없는 선석은 후보에서 제외됩니다 — "모르면 추천하지 않는다"는 원칙입니다.
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
