"""기상분석 에이전트 요청/응답 스키마.

안전관제 에이전트가 LLM으로 MSDS 근거 문장을 생성하는 것과 달리, 기상분석
에이전트는 계획서 명시대로(18p) LLM을 쓰지 않는 조건문 기반 룰 엔진이다.
응답 속도와 판단 일관성을 위해 결정적으로만 판단한다.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class WorkStatus(str, Enum):
    """작업 가능 여부 3단계 + 관측 데이터 부재 시의 4번째 상태.

    계획서는 "작업가능/조건부 가능/불가" 3단계만 명시하지만, 관측 데이터가
    없거나 오래된 경우 "가능"으로 낙관 판단하면 안전상 위험하므로 UNKNOWN을
    별도로 둔다 (모르면 가능하다고 하지 않는다 — scheduling 에이전트의
    depth_m 미상 선석 제외 원칙과 동일).
    """

    AVAILABLE = "가능"
    CONDITIONAL = "조건부가능"
    BLOCKED = "불가"
    UNKNOWN = "판단불가"


class WeatherAssessmentRequest(BaseModel):
    as_of: datetime | None = Field(
        default=None,
        description="판단 기준 시각(UTC). 생략 시 서버 현재 시각. 이 시각 이전의 "
        "최신 관측치로 '지금' 상태를 판단한다.",
    )
    expected_completion_at: datetime | None = Field(
        default=None,
        description="예상 하역 완료 시각(UTC). 지정하면 as_of~이 시각 사이의 단기예보"
        "(기상청 getVilageFcst, 최대 3일)를 조회해 작업 중 기상 악화 가능성을 사전 경고한다. "
        "생략하면 예보 비교 없이 현재 상태만 반환한다.",
    )

    @model_validator(mode="after")
    def _completion_after_as_of(self) -> "WeatherAssessmentRequest":
        if (
            self.expected_completion_at is not None
            and self.as_of is not None
            and self.expected_completion_at <= self.as_of
        ):
            raise ValueError("expected_completion_at은 as_of보다 이후여야 합니다.")
        return self


class ObservationFactor(BaseModel):
    """단일 관측 항목(풍속 또는 파고)의 값과 근거."""

    value: float | None = Field(description="관측값. 관측 없음이면 None")
    unit: str
    observed_at_utc: datetime | None = None
    station_name: str | None = None
    is_stale: bool = Field(description="관측이 없거나 기준 시각 대비 너무 오래된 경우 True")


class ForecastPoint(BaseModel):
    """단기예보 한 시각의 판단 결과 (작업 중 기상 악화 사전 경고용)."""

    fcst_at_utc: datetime
    status: WorkStatus
    wind_speed_ms: float | None
    wave_height_m: float | None


class ForecastWarning(BaseModel):
    """as_of ~ expected_completion_at 구간의 단기예보 종합."""

    window_end_utc: datetime
    forecast_points_checked: int = Field(description="구간 내 조회된 예보 시각 개수")
    will_deteriorate: bool = Field(description="구간 내에 조건부가능 이상으로 악화되는 시점이 있는지")
    worst_status: WorkStatus = Field(description="구간 내 예보 중 가장 심각한 상태")
    earliest_deterioration_at_utc: datetime | None = Field(
        default=None, description="조건부가능 이상으로 처음 악화되는 예보 시각"
    )
    points: list[ForecastPoint]


class WeatherAssessmentResult(BaseModel):
    status: WorkStatus
    assessed_at_utc: datetime = Field(description="실제로 사용한 판단 기준 시각(요청의 as_of 또는 현재 시각)")
    wind: ObservationFactor
    wave: ObservationFactor
    visibility_m: float | None = Field(
        default=None,
        description="참고용 시정 정보. 판단에는 사용하지 않음(알려진 한계 참고)",
    )
    reasons: list[str] = Field(description="상태 판단 근거 (임계값 비교 결과, 관측 결측 등)")
    forecast_warning: ForecastWarning | None = Field(
        default=None,
        description="expected_completion_at을 지정했을 때만 채워짐",
    )
