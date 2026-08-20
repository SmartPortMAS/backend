from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# app.agents.orchestrator.schemas.OverallDecision 중 "배정을 만들지 않는" 세 값만
# 여기 대상이다(APPROVED/WAITING_ANCHORAGE는 각각 berth_assignment/anchorage_queue로
# 감. 화물 미식별로 아예 판정을 안 돌린 건도 대상 아님 — chem_id가 없으면 orchestrate
# 자체를 호출하지 않으므로 "판정 결과"가 없다).
DECISION_NO_ELIGIBLE_BERTH = "NO_ELIGIBLE_BERTH"
DECISION_ALL_CANDIDATES_UNSAFE = "ALL_CANDIDATES_UNSAFE"
DECISION_WEATHER_BLOCKED = "WEATHER_BLOCKED"

ALL_DECISIONS = (DECISION_NO_ELIGIBLE_BERTH, DECISION_ALL_CANDIDATES_UNSAFE, DECISION_WEATHER_BLOCKED)


class SchedulingExclusion(Base):
    """자동 추천이 안 된(=berth_assignment도 anchorage_queue도 안 만들어진) 건의
    현재 상태 스냅샷. 08_스케줄링_전면재설계_자동배정_설계문서.md §5.3 3번이
    "관제 경고 센터가 노출한다"고만 해 두고 실제 연동은 남겨 뒀던 부분.

    call_sign을 유니크 키로 삼아 "이 배는 지금 왜 대기 중인가"를 배 1척당 최신
    상태 1행으로만 유지한다(이력 테이블이 아니다) — watch_arrivals가 10분마다
    돌 때마다 새 행을 쌓으면 관제 화면에 같은 배가 수십 건 중복 노출된다.
    배가 결국 배정되거나(APPROVED) 정박지 대기로 바뀌거나(WAITING_ANCHORAGE)
    입항 대상에서 사라지면(출항 등) watch_arrivals가 이 행을 지운다 — 자세한
    정리 규칙은 arrival_watcher.py 주석 참고.
    """

    __tablename__ = "scheduling_exclusion"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    call_sign: Mapped[str] = mapped_column(String, nullable=False, unique=True, comment="선박 호출부호")
    vessel_name: Mapped[str | None] = mapped_column(String)
    imo_no: Mapped[str | None] = mapped_column(String)
    cargo_chem_id: Mapped[str | None] = mapped_column(String, comment="msds_chemical.chem_id")

    decision: Mapped[str] = mapped_column(
        String, nullable=False,
        comment="NO_ELIGIBLE_BERTH/ALL_CANDIDATES_UNSAFE/WEATHER_BLOCKED "
        "(orchestrator.schemas.OverallDecision 영문 name)",
    )
    reason: Mapped[str | None] = mapped_column(String, comment="오케스트레이터 summary")
    draught_m: Mapped[float | None] = mapped_column(Float)

    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="최초 발생 시각")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="가장 최근에 같은 사유로 재확인된 시각"
    )
