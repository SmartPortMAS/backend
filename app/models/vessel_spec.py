from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.common import CollectorMetadataMixin


class VesselSpec(Base, CollectorMetadataMixin):
    """해양수산부 선박제원정보(SicsVsslManp3/Info3) 조회 결과. callsgn당 최신 1행 유지(upsert).

    08_스케줄링_전면재설계_자동배정_설계문서.md §5.2.1-A — 안벽 길이(berth.length_m) ↔
    선박 전장(loa_m) 게이트에 쓰기 위해 신설. 아직 하드 게이트로 연결되지 않았고
    (표본 확대 후 재검증 예정, §9 3-5~3-6), 이 테이블은 그 판단 근거를 쌓는 용도다.

    스키마 소유권은 backend(Alembic)에 있다. data-pipeline은 insert(upsert)만 한다.
    """

    __tablename__ = "vessel_spec"

    callsgn: Mapped[str] = mapped_column(String(20), primary_key=True, comment="호출부호(조회 키)")

    inout_port_se: Mapped[str | None] = mapped_column(String(50), comment="내외항구분(ibobprt)")
    vessel_no: Mapped[str | None] = mapped_column(String(50), comment="선박번호")
    imo_no: Mapped[str | None] = mapped_column(String(20), comment="IMO번호 — callsgn보다 안정적인 보조 조인 키")
    vessel_kor_name: Mapped[str | None] = mapped_column(String(200), comment="선박한글명")
    vessel_eng_name: Mapped[str | None] = mapped_column(String(200), comment="선박영문명")
    vessel_kind: Mapped[str | None] = mapped_column(String(100), comment="선박종류")
    vessel_nationality: Mapped[str | None] = mapped_column(String(100), comment="선박국적")

    ton_edyc_se: Mapped[str | None] = mapped_column(String(20), comment="톤수증서구분 코드")
    ton_edyc_se_name: Mapped[str | None] = mapped_column(String(100), comment="톤수증서구분명")
    intrl_gross_tonnage: Mapped[float | None] = mapped_column(Float, comment="국제총톤수")
    gross_tonnage: Mapped[float | None] = mapped_column(Float, comment="총톤수")
    net_tonnage: Mapped[float | None] = mapped_column(Float, comment="순톤수")

    loa_m: Mapped[float | None] = mapped_column(
        Float, comment="선박총길이(vsslTotLt) — berth.length_m과 직접 비교하는 이번 게이트의 핵심 값"
    )
    beam_m: Mapped[float | None] = mapped_column(Float, comment="선박너비(shdth)")
    draught_m: Mapped[float | None] = mapped_column(
        Float, comment="선박흘수(vsslDrft) — upa_vessel_position.draught 교차검증용 부수 소득, 이번 스코프 아님"
    )
    registered_length_m: Mapped[float | None] = mapped_column(
        Float, comment="선박길이(vsslLt) — 등록길이 계열로 추정, loa_m(총길이)과는 다른 값일 수 있음"
    )
    depth_m: Mapped[float | None] = mapped_column(Float, comment="선박깊이(vsslDp, molded depth) — berth.depth_m(수심)과 무관")

    bareboat_charter_se: Mapped[str | None] = mapped_column(String(20), comment="나용선구분 코드")
    bareboat_charter_se_name: Mapped[str | None] = mapped_column(String(100), comment="나용선구분명")
    operation_shape_cd: Mapped[str | None] = mapped_column(String(20), comment="운항형태 코드")
    operation_shape_name: Mapped[str | None] = mapped_column(String(100), comment="운항형태명")
    built_at: Mapped[str | None] = mapped_column(
        String(50), comment="선박건조일시(vsslCnstrDt) — 원본 포맷 미확인이라 우선 문자열로 보존"
    )
    prev_callsgn: Mapped[str | None] = mapped_column(String(20), comment="이전호출부호(befClsgn)")
    is_new_ship: Mapped[str | None] = mapped_column(
        String(10), comment="신조선여부(nwshipAt) — 값 표기(Y/N 등) 미확인이라 우선 문자열로 보존"
    )
