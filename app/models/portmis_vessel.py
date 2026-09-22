from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String
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
    is_liquid_cargo_barge: Mapped[bool | None] = mapped_column(Boolean, comment="액체화물 부선 여부")
    is_bunkering_vessel: Mapped[bool | None] = mapped_column(Boolean, comment="급유선 여부")

    # (2026-09-21) 입·출항 이벤트 컬럼 — 마이그레이션 0011/0018/0020이 DB에는 넣었으나
    # 이 모델에는 선언이 빠져 있었다. 그대로 두면 `alembic revision --autogenerate`가
    # 이 9개를 "모델에 없는 컬럼"으로 보고 op.drop_column()을 생성한다(실측: DB 51컬럼
    # vs 모델 37 + CollectorMetadataMixin 5 = 42). 하필 arrival_facility_nm/cd는
    # mart.arrival_schedule·mart.berth_draught_check·mart.anchorage_audit·
    # safety_index.py가 전부 읽는 값이라 유실되면 배정 정보가 통째로 날아간다.
    # 타입·길이는 실 DB 스키마와 1:1로 맞췄다(information_schema 대조).
    arrival_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="실제 입항 일시(arrival_etryptDt)"
    )
    arrival_facility_cd: Mapped[str | None] = mapped_column(
        String(20), comment="공식 배정 계선시설 코드(arrival_laidupFcltyCd)"
    )
    arrival_facility_nm: Mapped[str | None] = mapped_column(
        String(100), comment="공식 배정 계선시설 명(arrival_laidupFcltyNm). "
        "'정박지-E1' 같은 정박지 배정도 이 컬럼으로 온다"
    )
    departure_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="실제 출항 일시(departure_tkoffDt). "
        "scheduler.release_completed_berths의 선석 해제 근거"
    )
    departure_facility_cd: Mapped[str | None] = mapped_column(
        String(20), comment="출항 시점 계선시설 코드(departure_laidupFcltyCd)"
    )
    departure_facility_nm: Mapped[str | None] = mapped_column(
        String(100), comment="출항 시점 계선시설 명(departure_laidupFcltyNm)"
    )
    gross_tonnage: Mapped[int | None] = mapped_column(
        Integer, comment="총톤수 G/T(arrival_grtg). mart.anchorage_audit의 톤급 판정 축"
    )
    agency_name: Mapped[str | None] = mapped_column(
        String(200), comment="선박대리점(arrival_satmntEntrpsNm)"
    )
    arrival_report_type: Mapped[str | None] = mapped_column(
        String(10), comment="입항 신고구분(최초/변경/최종, arrival_reqstSeNm). "
        "mart.anchorage_audit.verdict_confidence가 예정/확정을 가르는 근거"
    )

    # (2026-09-13) 계선시설 서브코드 + 화물톤수 — raw에는 있었으나 전처리 COLUMN_MAP
    # 누락으로 그동안 버려지던 필드. 서브코드는 upa_port_call.facility_spec_sub_code와
    # 같은 체계라 시설코드 조인 정밀도를 wharf 단위에서 선석 단위로 올리는 데 필요.
    arrival_facility_sub_code: Mapped[str | None] = mapped_column(
        String(10), comment="입항 시점 계선시설 서브코드(arrival_laidupFcltySubCd). "
        "upa_port_call.facility_spec_sub_code와 같은 체계"
    )
    departure_facility_sub_code: Mapped[str | None] = mapped_column(
        String(10), comment="출항 시점 계선시설 서브코드(departure_laidupFcltySubCd)"
    )
    intrl_gross_tonnage: Mapped[float | None] = mapped_column(
        Numeric, comment="국제총톤수(arrival_intrlGrtg). gross_tonnage(국내 총톤수)와는 다른 값"
    )
    cargo_class_code: Mapped[str | None] = mapped_column(
        String(10), comment="화물명세 코드(arrival_ldadngFrghtClCd)"
    )
    cargo_onboard_ton: Mapped[float | None] = mapped_column(
        Numeric, comment="입항 시 적재 중인 화물 총톤수(arrival_ldadngTon)"
    )
    cargo_transship_ton: Mapped[float | None] = mapped_column(
        Numeric, comment="환적톤수(arrival_trnpdtTon)"
    )
    cargo_unload_ton: Mapped[float | None] = mapped_column(
        Numeric, comment="이 항에서 양하(하역)한 화물톤수 — 입항상세(arrival_landngFrghtTon)"
    )
    cargo_load_ton: Mapped[float | None] = mapped_column(
        Numeric, comment="이 항에서 적하(선적)한 화물톤수 — 출항상세(departure_ldFrghtTon)"
    )

    __table_args__ = (
        Index(
            "idx_portmis_vessel_natural_key",
            "callsgn", "entry_year", "entry_count",
            unique=True,
        ),
    )
