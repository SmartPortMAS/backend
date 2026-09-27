"""판정 이력 (D1) — 9/17 회의 §8, 방향 C.

이 표가 이 시스템의 **산출물**이다. `berth_assignment` 과 역할이 다르다.

    berth_assignment  = "이 배를 이 선석에 넣는다"  ← 배정. 우리는 하지 않는다.
    assessment_history = "이 배가 지금 있는 자리가 조건에 맞는가" ← 판정. 우리가 한다.

한 배는 입항 과정에서 여러 번 판정된다(입항전 → 접안직전 → 하역중). 시점이
바뀌어 등급이 달라지면 `changed_from` 에 직전 등급을 적어 **변화를 추적**한다.
회의 §5 가 멀티에이전트 차별점으로 꼽은 "시점별 재호출 · 변화 추적"이 이것이다.

[등급을 RiskLevel 과 분리한 이유]
  기존 `agents/safety/schemas.py::RiskLevel` 은 안전/주의/위험/**배정불가** 다.
  마지막 값이 배정 주체의 어휘다 — "배정할 수 없다"는 배정하는 쪽이 하는 말이다.
  방향 C 에서 우리는 배정하지 않으므로 `적합/주의/부적합/판정불가` 를 따로 둔다.
  한 Enum 에 섞으면 "우리가 배정한다"는 오해가 코드에 그대로 남는다.

[판정불가를 안전과 구분하는 이유]
  회의 §4 "근거 부족을 안전과 구분". 흘수를 모르는 것과 흘수가 충분한 것은 다르다.
  실측으로도 이 구분이 필요하다 — 오늘 백테스트 표본 125건 중 흘수 정보가 아예
  없는 건이 51건(40.8%)이었다. 이걸 '적합'으로 밀면 40%를 근거 없이 통과시킨다.
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AssessmentStage(str, Enum):
    """지금 어느 시점인가. **판정이 아니라 사실이다.**

    전이 트리거는 AIS 항해상태 하나뿐이다 — PORT-MIS 를 쓰지 않는다.

    PORT-MIS 수집창은 `[어제, 오늘+3일]`(portmis_collector.LOOKAHEAD_DAYS=3)이라
    **입항일이 이틀 이상 지난 건은 다시 조회되지 않고 그 시점 값에서 동결된다.**
    2026-09-21 실측: 열려 있는 액체화물선 79척 중 32척이 3일 초과 동결이었고,
    "입항 3일 초과 + 출항시각 없음 + 행 동결" 191건의 AIS 실제 상태는
    NO_SIGNAL 21 · PRESENT 9 · DEPARTED 1 · STALE 1 — 대부분 이미 나간 배다.

    출항은 물리적 사실이고 AIS 는 5분마다 갱신되지만, PORT-MIS 출항신고는
    조회창 밖이면 영영 오지 않는다. 그래서 시점은 AIS 로만 정한다.
    """

    BEFORE_ARRIVAL = "입항전"
    BEFORE_BERTHING = "접안직전"
    DURING_CARGO = "하역중"


class AssessmentLevel(str, Enum):
    """판정 등급. 값 순서가 곧 심각도 순서는 **아니다** — UNKNOWN 이 별도 축이다."""

    FIT = "적합"
    CAUTION = "주의"
    UNFIT = "부적합"
    UNKNOWN = "판정불가"


#: 게이트를 잠그는 등급 (회의 §8: "하역중 단계 부적합 또는 판정불가").
#: '모르면 닫는다' — weather/schemas.py 의 UNKNOWN 취급과 같은 원칙이다.
GATE_BLOCKING_LEVELS = (AssessmentLevel.UNFIT, AssessmentLevel.UNKNOWN)


class AssessmentAction(str, Enum):
    """조치안. **우리가 실행하지 않는다** — 권한 있는 곳에 넘길 내용이다(회의 §3)."""

    ALTERNATIVE_BERTH = "대체선석"
    ANCHORAGE_WAIT = "정박지대기"
    HOLD_ARRIVAL = "입항보류"
    HOLD_CARGO = "하역보류"


class AssessmentRecipient(str, Enum):
    """조치안을 받을 곳. 회의 §2 조사 결과 — 결정 주체가 셋으로 나뉘어 있다.

    1차 보고서가 이 셋을 "VTS 관제사가 배정을 승인"으로 뭉뚱그린 것이 근본 오류였다.
    """

    BERTH_OPERATOR = "선석운영주체"  # 항만공사 선석회의
    VTS = "VTS"  # 해경 — 항내 진입·이동 통제
    TERMINAL = "터미널"  # 하역 개시·중단


class AssessmentHistory(Base):
    """한 선박 · 한 시점의 판정 1건."""

    __tablename__ = "assessment_history"
    __table_args__ = (
        Index("ix_assessment_history_call_sign_time", "call_sign", "assessed_at_utc"),
        # 게이트(= /ws/hardware)가 매번 던지는 질의 — "이 선석의 최신 하역중 판정".
        Index("ix_assessment_history_wharf_time", "wharf_name", "assessed_at_utc"),
        {"comment": "판정 이력(D1). 배정이 아니라 '지금 자리가 조건에 맞는가'의 기록."},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    call_sign: Mapped[str] = mapped_column(String, nullable=False, index=True)
    vessel_name: Mapped[str | None] = mapped_column(String)
    stage: Mapped[str] = mapped_column(String(20), nullable=False, comment="입항전|접안직전|하역중")
    wharf_name: Mapped[str | None] = mapped_column(
        String, comment="판정 대상 계류시설(정규화 후). 정박지 배정·미해소 표기면 NULL"
    )
    level: Mapped[str] = mapped_column(String(20), nullable=False, comment="적합|주의|부적합|판정불가")

    axes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict,
        comment='축별 등급·근거 {"기상": {...}, "흘수": {...}, "혼재": {...}, "점유": {...}}',
    )
    reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, comment="사람이 읽는 근거 문장"
    )

    action: Mapped[str | None] = mapped_column(String(20), comment="대체선석|정박지대기|입항보류|하역보류")
    action_detail: Mapped[dict | None] = mapped_column(JSONB, comment="대체 선석 후보·정박지 등")
    recipient: Mapped[str | None] = mapped_column(String(20), comment="선석운영주체|VTS|터미널")
    graph_path: Mapped[list[str] | None] = mapped_column(ARRAY(Text), comment="D3 근거 경로 문장")

    changed_from: Mapped[str | None] = mapped_column(
        String(20), comment="같은 선박·같은 stage 직전 판정의 level. 변화가 없으면 애초에 기록하지 않는다"
    )

    input_snapshot: Mapped[dict | None] = mapped_column(
        JSONB,
        comment=(
            "판정에 쓴 관측의 시각과 값(재현용). portmis_collected_at 을 반드시 담는다 — "
            "PORT-MIS 는 수집창 밖이면 동결되므로, 이 값이 없으면 '며칠 묵은 배정으로 "
            "내려진 판정인가'를 사후에 가릴 방법이 없다"
        ),
    )

    assessed_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # B3 "확인·조치 기록" — 승인이 아니다. 관제사가 이 판정을 봤다는 사실만 남긴다.
    # 이 값이 채워져도 어떤 자원도 잠기지 않고 어떤 배정도 확정되지 않는다.
    acknowledged_by: Mapped[str | None] = mapped_column(String)
    acknowledged_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
