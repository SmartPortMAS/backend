from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, String, Text, text
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

    # ── 정형 값 컬럼 (Alembic 0007) ──────────────────────────────────────────
    # msds_payload 안의 "값 하나짜리" 항목을 꺼내 승격한 것. 벡터 검색으로 근사
    # 조회하던 값들을 결정적으로 답하기 위한 컬럼이라, 챗봇 프롬프트의 [근거]
    # 블록에 그래프 프로필과 나란히 실린다(chatbot/prompt.py _format_profile).
    #
    # 값이 없는 항목은 NULL이다 — KOSHA의 "자료없음"/"해당없음"은 적재 시점에
    # NULL로 정규화한다. "자료없음"을 그대로 두면 LLM이 값처럼 인용한다.
    #
    # IMDG 등급·GHS 분류·UN번호는 여기 없다. 앞의 둘은 Neo4j 관계(HAS_IMDG_CLASS,
    # HAS_HAZARD)로 이미 있고 관계 순회가 필요한 값이며, UN번호는 un_no 컬럼이다.
    flash_point_text: Mapped[str | None] = mapped_column(String(300), comment="detail09 I14")
    flash_point_celsius: Mapped[float | None] = mapped_column(
        Float, comment="flash_point_text에서 파싱한 섭씨값. 부등호/범위 표기는 파싱 실패해 NULL"
    )
    boiling_point_text: Mapped[str | None] = mapped_column(String(300), comment="detail09 I12")
    vapor_pressure_text: Mapped[str | None] = mapped_column(String(300), comment="detail09 I22")
    specific_gravity_text: Mapped[str | None] = mapped_column(String(300), comment="detail09 I28")
    packing_group: Mapped[str | None] = mapped_column(String(20), comment="detail14 N08 용기등급")
    ems_fire: Mapped[str | None] = mapped_column(String(20), comment="detail14 N1202 화재 EMS")
    ems_spill: Mapped[str | None] = mapped_column(String(20), comment="detail14 N1204 유출 EMS")
    signal_word: Mapped[str | None] = mapped_column(String(50), comment="detail02 B0404 신호어")
    exposure_limit_kr: Mapped[str | None] = mapped_column(
        Text, comment="detail08 H0202 국내 노출기준(TWA/STEL)"
    )

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
