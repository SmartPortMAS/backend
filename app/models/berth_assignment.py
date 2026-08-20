from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

STATUS_REQUESTED = "REQUESTED"
STATUS_APPROVED = "APPROVED"
STATUS_SCHEDULED = "SCHEDULED"
STATUS_BERTHED = "BERTHED"
STATUS_COMPLETED = "COMPLETED"
STATUS_CANCELLED = "CANCELLED"
STATUS_REJECTED = "REJECTED"

ALL_STATUSES = (
    STATUS_REQUESTED,
    STATUS_APPROVED,
    STATUS_SCHEDULED,
    STATUS_BERTHED,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
    STATUS_REJECTED,
)

# EXCLUDE 제약(같은 선석·슬롯·시간대 중복 배정 차단)의 대상이 되는 상태.
# CANCELLED/COMPLETED/REJECTED는 자원을 더 이상(또는 애초에) 점유하지 않으므로
# 여기서 빠진다 — 취소된 예약이 같은 시간대 재예약을 영구히 막으면 안 되고,
# REJECTED는 berth_id/planned_window 자체가 NULL일 수 있어 겹침 검사 대상이 아니다.
ACTIVE_STATUSES = (STATUS_REQUESTED, STATUS_APPROVED, STATUS_SCHEDULED, STATUS_BERTHED)


class BerthAssignment(Base):
    """선석 점유/예약 기록. 관제사 승인·스케줄링 에이전트 배정을 시간구간
    (planned_window)으로 저장하는 이 프로젝트의 핵심 테이블 — "판정은 있지만
    기록이 없다"는 공백을 메우는 자리. 같은 선석·슬롯의 활성 예약끼리 겹치면
    DB 레벨 EXCLUDE 제약이 막는다.

    동시접안 N척 가능한 선석(소형 선석 등)은 슬롯 분해로 처리한다 — `upa_berth_facility`
    행을 쪼개지 않고 `slot_no`(1..`upa_berth_facility.berth_vessel_count`)를 EXCLUDE
    제약의 등가 키에 포함시켜, 같은 선석의 서로 다른 슬롯은 독립적으로 겹칠 수 있게
    한다. 동시성 안전장치가 애플리케이션의 락 규율이 아니라 DB 제약 자체에
    있어, 어떤 경로로 INSERT하든(오케스트레이터/관리자 도구/향후 배치 스크립트)
    우회가 불가능하다.

    `available_count`/`is_occupied` 같은 잔여수량·불리언 컬럼은 의도적으로 두지
    않는다 — 선석은 시간 축을 가진 자원이므로 시간구간 예약(tstzrange + EXCLUDE)
    모델로만 다룬다.
    """

    __tablename__ = "berth_assignment"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in ALL_STATUSES) + ")",
            name="ck_berth_assignment_status",
        ),
        ExcludeConstraint(
            ("berth_id", "="),
            ("slot_no", "="),
            ("planned_window", "&&"),
            using="gist",
            where=text("status IN (" + ", ".join(f"'{s}'" for s in ACTIVE_STATUSES) + ")"),
            name="ex_berth_assignment_no_overlap",
        ),
        Index("idx_berth_assignment_berth_id", "berth_id"),
        Index("idx_berth_assignment_vessel_uid", "vessel_uid"),
        {
            "comment": "선석 점유/예약 기록. 관제사 승인·스케줄링 에이전트 배정을 시간구간(planned_window)"
            "으로 저장하는 이 프로젝트의 핵심 테이블 — \"판정은 있지만 기록이 없다\"는 공백을 메우는 자리."
            " 같은 선석·슬롯의 활성 예약끼리 겹치면 DB 레벨 EXCLUDE 제약이 막음",
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="PK")

    # upa_berth_facility.wharf_name FK(DB 레벨, 0016 마이그레이션에서 raw DDL로 건다).
    # upa_berth_facility는 data-pipeline이 소유한 테이블이라 여기서 ORM ForeignKey()를
    # 선언하지 않는다 — 다른 UPA raw 테이블(upa_port_call 등)과 같은 패턴.
    berth_id: Mapped[str | None] = mapped_column(
        String, nullable=True,
        comment="배정된 선석 ID(upa_berth_facility.wharf_name FK, DB 레벨). "
        "예약이 성립하지 않은 반려(REJECTED) 건은 NULL일 수 있음",
    )
    slot_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
        comment="1..upa_berth_facility.berth_vessel_count. 동시접안 슬롯 번호",
    )

    call_sign: Mapped[str | None] = mapped_column(String, comment="선박 호출부호. 입항 전 사전 승인 단계엔 이것만 있을 수 있음")
    imo_no: Mapped[str | None] = mapped_column(String, comment="선박 IMO 번호")
    vessel_name: Mapped[str | None] = mapped_column(String, comment="선박명")
    vessel_uid: Mapped[str | None] = mapped_column(
        String,
        comment="파이프라인 표준 선박 식별자(MMSI-First). 사전신고 시점엔 call_sign/"
        "vessel_name만 있을 수 있어 nullable — AIS로 확인되면 후속 채움",
    )

    cargo_chem_id: Mapped[str | None] = mapped_column(String, comment="화물 화학물질 식별자(msds_chemical.chem_id)")
    cargo_cas_no: Mapped[str | None] = mapped_column(String, comment="화물 CAS 등록번호")

    planned_window = mapped_column(
        TSTZRANGE, nullable=True,
        comment="계획된 접안 시간구간(TSTZRANGE, [접안예정~출항예정)). 같은 berth_id·slot_no에서 활성 "
        "상태끼리 겹치면 EXCLUDE 제약이 INSERT를 막음. REJECTED 건은 NULL일 수 있음",
    )
    actual_berthing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="실제 접안이 확인된 시각(계획이 아니라 사후 확인값 — upa_port_call 등으로 대조)"
    )
    actual_departure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="실제 출항이 확인된 시각"
    )

    status: Mapped[str] = mapped_column(
        String, nullable=False, default=STATUS_REQUESTED,
        comment="예약 상태: REQUESTED(요청)/APPROVED(승인)/SCHEDULED(배정확정)/BERTHED(접안중)/"
        "COMPLETED(완료)/CANCELLED(취소)/REJECTED(반려). REQUESTED~BERTHED만 겹침 방지 대상(ACTIVE_STATUSES)",
    )
    approved_by: Mapped[str | None] = mapped_column(String)

    assignment_score: Mapped[float | None] = mapped_column(
        Float, comment="배정 알고리즘 점수. 이번 라운드는 컬럼만 — 채우는 로직은 후속(C단계)"
    )
    assignment_reason: Mapped[str | None] = mapped_column(
        Text, comment="배정/승인/반려 판단 사유 설명(관제사 조회 및 감사용, 오케스트레이터 summary 등을 담음)"
    )
    rejected_candidates: Mapped[dict | None] = mapped_column(
        JSONB, comment="검토했으나 탈락한 후보와 탈락 사유 (설명가능성 요구사항)"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="레코드 생성 시각(=결정이 내려진 시각)"
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="레코드 수정 시각")
