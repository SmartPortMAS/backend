from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.weather.schemas import WeatherAssessmentRequest, WeatherAssessmentResult
from app.agents.weather.service import assess_weather
from app.core.deps import get_session

router = APIRouter(prefix="/weather", tags=["weather"])

_DESCRIPTION = """
현재(또는 지정 시각) 관측 기상으로 **하역 작업 가능 여부**를 판정합니다.
**LLM을 쓰지 않습니다** — 임계값 비교만 하는 결정적 룰엔진이라 같은 입력이면 항상
같은 결과가 나옵니다.

### `berth_group`을 보내는 것이 핵심입니다

임계값은 전역 단일 값이 아니라 **부두그룹별**입니다. 같은 바람이라도 정일부두는
하역중단인데 OTK부두는 정상일 수 있습니다(정일 17m/s vs OTK 14m/s).

`berth_group`을 생략하면 전역 폴백 값을 쓰므로 이 차별점이 발동하지 않습니다.
값은 선석 조회 응답의 `berth_group`(예: `"OTK1/2부두(처용리)"`)을 그대로 넘기세요.

`berth_group`이 `null`인 선석은 임계값 자료가 없다는 뜻이고, 이때도 전역 폴백으로
판정됩니다.

### `status` 읽는 법 — 4단계 에스컬레이션

| 값 | 의미 |
|---|---|
| `정상` | 작업 가능 |
| `하역중단` | 하역을 멈춰야 함 |
| `이안` | 선박을 안벽에서 떼어내야 함 |
| `호스분리` | 하역 호스를 분리해야 함 (가장 심각) |
| `판단불가` | 관측치가 없거나 오래됨 |

**`정상`이 아니면 전부 "지금 하역하면 안 되는 상태"입니다.** 중간 완충 구간(조건부
가능)은 없습니다.

`판단불가`를 "괜찮다"로 읽으면 안 됩니다. 관측치 결측이나 3시간 초과된 낡은 데이터,
또는 부두그룹 임계값 미등록 상태이며 **가장 보수적으로 다뤄야 합니다.**

### 수동 입력 두 가지

자동 판정 소스가 없어 관제사 입력으로 받습니다.

- `precip_observed` — 육안으로 확인한 강수 여부. 항만기상정보시스템 API에 강수량
  실황 필드가 없습니다. `true`면 최소 `하역중단`으로 반영하되, 정확한 mm를 모르므로
  그 이상 단계로 자동 격상하지는 않습니다
- `extra_condition_active` — 대기정체·심한뇌우·태풍경로 등 정성 조건

### 그 외

- `reasons` — 판정 근거 문장 목록(임계값 비교 결과, 결측 사유). 화면에 그대로 노출하세요
- `forecast_warning` — `expected_completion_at`을 보냈을 때만 채워집니다. 하역 중
  기상이 악화될 예보가 있으면 미리 경고합니다
- `visibility_m` — **참고용이며 판정에 쓰지 않습니다**
"""

_RESPONSES: dict = {
    422: {"description": "요청 형식 오류"},
}


@router.post(
    "/assess",
    response_model=WeatherAssessmentResult,
    summary="기상 기반 하역 작업 가능 여부 판정",
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
async def assess(
    request: WeatherAssessmentRequest,
    db: AsyncSession = Depends(get_session),
) -> WeatherAssessmentResult:
    return await assess_weather(db, request)
