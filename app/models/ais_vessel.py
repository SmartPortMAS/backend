from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.common import CollectorMetadataMixin


class AisVesselPosition(Base, CollectorMetadataMixin):
    """AIS 선박 위치 시계열. mmsi+received_at_utc 스냅샷을 이력으로 계속 쌓는다.

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 insert만 한다
    (record_uid 기준 upsert — 동일 내용 재수집은 무시, 내용이 바뀌면 새 행 추가).
    """

    __tablename__ = "ais_vessel_position"

    record_uid: Mapped[str] = mapped_column(String(32), primary_key=True)

    mmsi: Mapped[int | None] = mapped_column(Integer)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    sog: Mapped[float | None] = mapped_column(Float)
    cog: Mapped[float | None] = mapped_column(Float)
    nav_status_code: Mapped[str | None] = mapped_column(String(10))
    nav_status_category: Mapped[str | None] = mapped_column(String(20))
    received_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ulsan_bound: Mapped[bool | None] = mapped_column(Boolean)
