from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# berth_group이 지정되지 않은 호출(과거 전역 임계값 호출부와의 하위 호환)을 위한
# 폴백 키. app.agents.weather.data_access가 이 상수를 그대로 참조한다.
GLOBAL_DEFAULT_BERTH_GROUP = "__GLOBAL_DEFAULT__"


class BerthWeatherThreshold(Base):
    """부두그룹별 기상 임계값(중단/이안/호스분리 3단계 에스컬레이션).

    출처: 온산 MVP(feature/onsan-mvp)의 berth_weather_thresholds.csv — 터미널
    입항정보 PDF 9.8절 원문. 팀원이 처음 만든 CSV 중 기존 시스템에 대응 데이터가
    전혀 없던(9장 비교분석 참고) 유일한 신규 정적 데이터라 이 테이블로 그대로 이식한다.

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 insert만 한다
    (loaders/berth_weather_threshold_pg_loader.py, 1회성 시드).
    """

    __tablename__ = "berth_weather_threshold"

    berth_group: Mapped[str] = mapped_column(String(100), primary_key=True)
    operator: Mapped[str | None] = mapped_column(String(100))

    stop_wind_ms: Mapped[float | None] = mapped_column(Float)
    stop_wave_m: Mapped[float | None] = mapped_column(Float)
    unberth_wind_ms: Mapped[float | None] = mapped_column(Float)
    unberth_wave_m: Mapped[float | None] = mapped_column(Float)
    disconnect_wind_ms: Mapped[float | None] = mapped_column(Float)
    disconnect_wave_m: Mapped[float | None] = mapped_column(Float)

    extra_conditions: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(100))
    collected_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
