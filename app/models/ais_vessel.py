from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

# ais_preprocessor.py 가 정적 정보의 목적지 컬럼을 다른 컬럼과 달리 "Destination"으로
# (스네이크 케이스로 정규화하지 않고) 그대로 출력한다. insert가 DataFrame 컬럼명을 그대로
# 써야 하므로 DB 컬럼명도 동일하게 맞춘다 — 전처리기 쪽 정규화는 별도 정리 필요.

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


class AisVesselStatic(Base, CollectorMetadataMixin):
    """AIS 선박 정적 정보. mmsi 기준 최신 1행만 유지(제원/목적지 변경 시 덮어씀).

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 insert만 한다.
    """

    __tablename__ = "ais_vessel_static"

    mmsi: Mapped[int] = mapped_column(Integer, primary_key=True)

    imo_no: Mapped[str | None] = mapped_column(String(20))
    callsgn: Mapped[str | None] = mapped_column(String(20))
    vessel_name: Mapped[str | None] = mapped_column(String(200))
    ship_type: Mapped[str | None] = mapped_column(String(10))
    length: Mapped[float | None] = mapped_column(Float)
    width: Mapped[float | None] = mapped_column(Float)
    draught: Mapped[float | None] = mapped_column(Float)
    destination: Mapped[str | None] = mapped_column("Destination", String(200))
    ulsan_bound: Mapped[bool | None] = mapped_column(Boolean)
    is_liquid_cargo_vessel: Mapped[bool | None] = mapped_column(Boolean)
