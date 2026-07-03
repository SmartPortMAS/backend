from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MsdsChemical(Base):
    """KOSHA MSDS 화학물질 정보. data-pipeline 배치 수집과 backend 실시간 조회가 공유하는 테이블.

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 이 테이블에 insert만 한다.
    """

    __tablename__ = "msds_chemical"

    chem_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    cas_no: Mapped[str | None] = mapped_column(String(50))
    un_no: Mapped[str | None] = mapped_column(String(20))
    name_ko: Mapped[str | None] = mapped_column(String(500))
    name_en: Mapped[str | None] = mapped_column(String(500))

    source_system: Mapped[str] = mapped_column(String(50), nullable=False, default="KOSHA_MSDS_API")
    source_table: Mapped[str] = mapped_column(String(50), nullable=False, default="getChemDetail")
    collected_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quality_flag: Mapped[str] = mapped_column(String(20), nullable=False, default="OK")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    msds_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index(
            "idx_msds_chemical_cas_no",
            "cas_no",
            unique=True,
            postgresql_where=text("cas_no IS NOT NULL AND cas_no <> ''"),
        ),
        Index("idx_msds_chemical_payload_gin", "msds_payload", postgresql_using="gin"),
        Index("idx_msds_chemical_collected_at", "collected_at_utc"),
    )
