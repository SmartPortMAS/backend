"""berth_assignment EXCLUDE 제약에서 REQUESTED 제외 (점유 모델 A)

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-21

───────────────────────────────────────────────────────────────────────────────
점유 모델 A 채택 — "관측 기반, 추천만"
───────────────────────────────────────────────────────────────────────────────

이 시스템이 선석에 대해 가진 권한을 다음과 같이 확정한다:

    PORT-MIS 가 이미 선석을 배정해 내려보낸다. 우리 역할은 그 배정이 이 배의
    조건(흘수·화물·기상)에 맞는지 확인하고, 안 맞을 때 대체 선석을 **제안**하는
    것이다. 선석을 우리가 정하지도, 점령하지도 않는다.

그런데 지금 제약은 정반대로 동작한다.

    EXCLUDE USING gist (berth_id =, slot_no =, planned_window &&)
      WHERE (status IN ('REQUESTED','APPROVED','SCHEDULED','BERTHED'))

`REQUESTED` 는 **관제사 승인 전 단계의 자동 추천**이다. 그 행이 들어가는
순간 DB 가 그 선석·슬롯·시간대를 물리적으로 잠근다. 즉 아무도 승인하지 않은
기계의 제안이 실제 자원을 선점한다 — "점령하지 않는다"와 정면으로 어긋난다.

실제 코드도 그 전제로 쓰여 있었다(occupancy.py docstring):
    "이 스케줄링 에이전트가 선석을 직접 배정하는 주체이므로,
     점유 여부도 그 배정 기록 스스로가 기준이어야 한다"

이 마이그레이션은 그 전제를 바꾼다. 잠그는 것은 **사람이 승인한 뒤부터**다.

    변경 전: REQUESTED, APPROVED, SCHEDULED, BERTHED
    변경 후:            APPROVED, SCHEDULED, BERTHED

───────────────────────────────────────────────────────────────────────────────
부작용과 그 처리
───────────────────────────────────────────────────────────────────────────────
  · 같은 선석에 서로 다른 배의 REQUESTED 추천이 동시에 존재할 수 있게 된다.
    DB 는 더 이상 막지 않는다 — 추천은 자원 점유가 아니므로 그게 맞다.
    다만 추천이 한 선석에 몰리면 화면에서 쓸모가 없으므로, 응용 계층
    (occupancy.find_free_slot)은 REQUESTED 를 여전히 **소프트 회피** 대상으로
    본다. DB 제약(강제)과 추천 분산(권고)을 분리하는 것이 요지다.

  · 승인 시점(REQUESTED -> APPROVED)에는 제약이 새로 걸린다. 그 사이에 다른
    배가 같은 슬롯을 승인받았다면 여기서 IntegrityError 가 난다. 이건 숨길
    문제가 아니라 **드러나야 할 경합**이다 — 승인 API 가 그 예외를 잡아
    "그 선석은 방금 다른 배에 확정되었습니다"로 돌려주면 된다.

  · 기존 데이터: 적용 시점 berth_assignment 는 0행이라(실측 2026-09-21)
    제약 재생성으로 깨질 행이 없다. 행이 있는 환경에서는 제약이 느슨해지는
    방향이라 재생성이 실패하지 않는다.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 승인 이후 단계만 자원을 잠근다. REQUESTED(미승인 자동 추천)는 제외.
_LOCKING_STATUSES = ("APPROVED", "SCHEDULED", "BERTHED")
# 변경 전 값 — downgrade 에서 그대로 되돌린다.
_PREV_STATUSES = ("REQUESTED", "APPROVED", "SCHEDULED", "BERTHED")

_CONSTRAINT = "ex_berth_assignment_no_overlap"


def _recreate(statuses: tuple) -> None:
    op.execute(f"ALTER TABLE berth_assignment DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        "ALTER TABLE berth_assignment "
        f"ADD CONSTRAINT {_CONSTRAINT} "
        "EXCLUDE USING gist ("
        "    berth_id WITH =,"
        "    slot_no WITH =,"
        "    planned_window WITH &&"
        ") WHERE (status IN (" + ", ".join(f"'{s}'" for s in statuses) + "))"
    )


def upgrade() -> None:
    _recreate(_LOCKING_STATUSES)
    op.execute(
        "COMMENT ON TABLE berth_assignment IS "
        "'선석 배정 기록. REQUESTED 는 관제사 승인 전 **추천**이라 자원을 잠그지 않는다 — "
        "EXCLUDE 제약은 APPROVED 이후에만 적용된다(0025, 점유 모델 A). "
        "물리적 접안 현황은 mart.berth_occupancy_live 가 정본이다'"
    )


def downgrade() -> None:
    _recreate(_PREV_STATUSES)
