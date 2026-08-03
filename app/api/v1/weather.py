from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.weather.schemas import WeatherAssessmentRequest, WeatherAssessmentResult
from app.agents.weather.service import assess_weather
from app.core.deps import get_session

router = APIRouter(prefix="/weather", tags=["weather"])

_DESCRIPTION = """
현재 관측 기상으로 하역 가능 여부를 판정합니다. LLM을 쓰지 않는 결정적 룰엔진입니다.

- **`berth_group`을 보내세요.** 임계값이 부두그룹별이라, 생략하면 전역 폴백을 씁니다
  (같은 바람에 정일 17m/s · OTK 14m/s로 판정이 갈립니다). 값은 선석 응답의
  `berth_group`을 그대로 넘기면 됩니다.
- **`status`**: `정상` < `하역중단` < `이안` < `호스분리`. **`정상`이 아니면 전부 작업
  중단**이며 중간 완충 구간은 없습니다.
- **`판단불가`**는 관측 결측·3시간 초과 노후·임계값 미등록입니다. "괜찮다"가 아니라
  가장 보수적으로 다루세요.
- `precip_observed`·`extra_condition_active`는 자동 판정 소스가 없어 관제사가 직접
  넣는 값입니다.
- `reasons`는 판정 근거 문장이라 화면에 그대로 노출하면 됩니다.
  `visibility_m`은 참고용이며 판정에 쓰지 않습니다.

배경: `03_기상분석_에이전트_설계문서.md`
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
