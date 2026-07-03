from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.common import CollectorMetadataMixin


class PortmisVessel(Base, CollectorMetadataMixin):
    """PORT-MIS 입출항 신고 정보. callsgn+entry_year+entry_count 로 upsert(최신 상태 유지).

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 insert만 한다.
    """

    __tablename__ = "portmis_vessel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    port_agency_cd: Mapped[str | None] = mapped_column(String(20))
    port_agency_nm: Mapped[str | None] = mapped_column(String(100))
    port_agency_label: Mapped[str | None] = mapped_column(String(100))
    entry_year: Mapped[int | None] = mapped_column(Integer)
    entry_count: Mapped[int | None] = mapped_column(Integer)
    callsgn: Mapped[str | None] = mapped_column(String(20))
    vessel_name: Mapped[str | None] = mapped_column(String(200))
    nationality_cd: Mapped[str | None] = mapped_column(String(10))
    nationality_nm: Mapped[str | None] = mapped_column(String(100))
    ship_kind_cd: Mapped[str | None] = mapped_column(String(10))
    ship_kind_nm: Mapped[str | None] = mapped_column(String(100))
    ship_kind_category: Mapped[str | None] = mapped_column(String(50))
    entry_purpose_cd: Mapped[str | None] = mapped_column(String(10))
    entry_purpose_nm: Mapped[str | None] = mapped_column(String(100))
    origin_port_cd: Mapped[str | None] = mapped_column(String(20))
    origin_port_nm: Mapped[str | None] = mapped_column(String(100))
    prev_port_cd: Mapped[str | None] = mapped_column(String(20))
    prev_port_nm: Mapped[str | None] = mapped_column(String(100))
    next_port_cd: Mapped[str | None] = mapped_column(String(20))
    next_port_nm: Mapped[str | None] = mapped_column(String(100))
    dest_port_cd: Mapped[str | None] = mapped_column(String(20))
    dest_port_nm: Mapped[str | None] = mapped_column(String(100))
    departure_sched_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dest_arrival_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_liquid_cargo_vessel: Mapped[bool | None] = mapped_column(Boolean)
    is_domestic_voyage: Mapped[bool | None] = mapped_column(Boolean)

    __table_args__ = (
        Index(
            "idx_portmis_vessel_natural_key",
            "callsgn", "entry_year", "entry_count",
            unique=True,
        ),
    )
