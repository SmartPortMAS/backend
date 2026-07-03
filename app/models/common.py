from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column


class CollectorMetadataMixin:
    """data-pipeline common_preprocessing.add_common_metadata() 가 붙이는 공통 컬럼.

    AIS/PORTMIS/tide/wave/weather 등 data-pipeline이 수집·적재하는 모든 테이블에 공통 적용.
    """

    source_system: Mapped[str] = mapped_column(String(50), nullable=False)
    source_table: Mapped[str] = mapped_column(String(50), nullable=False)
    collected_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quality_flag: Mapped[str] = mapped_column(String(20), nullable=False, default="OK")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
