"""add portmis_vessel.arrival_report_type (입항 신고구분: 최초/변경/최종)

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-17

수집기가 오늘~+3일 창을 매번 다시 조회하도록 바뀌면서(portmis_collector.py
LOOKAHEAD_DAYS) 이 표에 "아직 입항하지 않은 예정 신고"가 들어온다. 같은 항차가
최초 → 변경 → 최종 신고로 여러 번 갱신되므로, 화면·에이전트가 지금 보는 값이
확정인지 예정인지 구분할 수 있어야 한다 — PORT-MIS 원본 arrival_reqstSeNm 을
그대로 보존한다.

arrival_at_utc 는 신고구분이 최종이 아니면 "입항 예정 시각"이다(같은 컬럼을
재사용한다 — 원본 API 도 같은 필드 etryptDt 에 예정/실제를 담는다).

실측(2026-09-17, prtAgCd=820, 오늘~+3일): 115건 중 입항 시각이 미래인 건 92건,
그중 액체화물선 65척 전부 계류시설 기재.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "portmis_vessel",
        sa.Column(
            "arrival_report_type", sa.String(length=10), nullable=True,
            comment="입항 신고구분(arrival_reqstSeNm): 최초/변경/최종. 최종이 아니면 arrival_at_utc 는 예정 시각",
        ),
    )


def downgrade() -> None:
    op.drop_column("portmis_vessel", "arrival_report_type")
