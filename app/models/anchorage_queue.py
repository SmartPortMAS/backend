from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

STATUS_WAITING = "WAITING"
STATUS_PROMOTED = "PROMOTED"
STATUS_CANCELLED = "CANCELLED"

ALL_STATUSES = (STATUS_WAITING, STATUS_PROMOTED, STATUS_CANCELLED)


class AnchorageQueue(Base):
    """정박지 대기열. 08_스케줄링_전면재설계_자동배정_설계문서.md §4.3 — `berth_assignment`와
    분리된 별도 테이블이다. `REQUESTED`는 이제 "선석이 추천됐고 관제사 승인을 기다리는
    중"이라는 구체적 의미를 가지므로(§5.3), `berth_id`가 없는 "정박지 대기 중"을 같은
    값으로 표현하면 의미가 충돌한다. 정박지는 잠금 대상이 아니므로(§4.3, UPA 정박지
    API에 수용 척수 데이터 자체가 없음) 이 테이블은 감사·추적용 기록일 뿐, EXCLUDE
    제약 같은 자원 잠금 장치를 두지 않는다.
    """

    __tablename__ = "anchorage_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    call_sign: Mapped[str | None] = mapped_column(String, comment="선박 호출부호")
    vessel_name: Mapped[str | None] = mapped_column(String)
    imo_no: Mapped[str | None] = mapped_column(String)

    cargo_chem_id: Mapped[str | None] = mapped_column(String, comment="msds_chemical.chem_id")
    cargo_cas_no: Mapped[str | None] = mapped_column(String)

    dwt_t: Mapped[float | None] = mapped_column(Float, comment="선박 DWT — 정박지 톤수 매칭에 사용")
    draught_m: Mapped[float | None] = mapped_column(Float)

    anchorage_id: Mapped[str | None] = mapped_column(
        String, comment="Neo4j Anchorage.id — select_anchorage_for_dwt() 매칭 결과"
    )

    entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        comment="대기열 진입 시각. anchorage_promoter가 FCFS 우선순위로 쓰는 기준(§5.4, §5.6)",
    )
    window_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="원래 희망 접안 시작 시각(오케스트레이터 최초 요청 그대로 보존) — "
        "승격 시 resolve_berth_assignment 재검증에 그대로 재사용(§5.4)",
    )
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(
        String, nullable=False, default=STATUS_WAITING,
        comment="WAITING(대기 중)/PROMOTED(빈 슬롯으로 승격됨)/CANCELLED(취소)",
    )
    promoted_berth_assignment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("berth_assignment.id", ondelete="SET NULL"),
        comment="승격 시 새로 만들어진 berth_assignment 행(status=REQUESTED)의 id. "
        "승격돼도 이 행 자체는 삭제하지 않는다 — 감사 이력",
    )

    assignment_reason: Mapped[str | None] = mapped_column(
        String, comment="정박지 대기로 귀결된 사유(오케스트레이터 summary 등)"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
