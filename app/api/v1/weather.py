from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.weather.schemas import WeatherAssessmentRequest, WeatherAssessmentResult
from app.agents.weather.service import assess_weather
from app.core.deps import get_session

router = APIRouter(prefix="/weather", tags=["weather"])


@router.post("/assess", response_model=WeatherAssessmentResult)
async def assess(
    request: WeatherAssessmentRequest,
    db: AsyncSession = Depends(get_session),
) -> WeatherAssessmentResult:
    return await assess_weather(db, request)
