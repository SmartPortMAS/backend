"""wharf.depth_floored — 선석별 수심을 믿을 수 없는 부두 표시

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-20

UPA 웹 부두현황이 **얕은 선석의 수심을 빠뜨린 부두**가 있다. 해수청 일반현황과
대조해 확인했다:

    부두        웹    공공 API   해수청 원본
    2부두       12       9        9~12
    용연부두     14      12       12~14
    신항컨부두   12      14       12~14
    SK5부두     11       7        7~11

네 건 모두 API·해수청이 옳고 웹이 깊은 쪽만 적었다. 그래서 wharf.min_water_depth_m
는 API 값으로 **얕은 쪽으로만** 끌어내린다(build_berth_seed._floor_depth_with_api).

문제는 그 다음이다. PORT-MIS 가 선석을 특정하면 감사는 berth.water_depth_m(웹 값)
을 쓰는데, 그 값이 바로 신뢰할 수 없는 값이다. 실측: 2부두 1~3선석이 전부 12m 로
판정돼 보정이 우회됐다.

어느 선석이 얕은지는 알 수 없으므로, 이 플래그가 켜진 부두는 **어느 선석이든
부두 최저수심으로 판정**한다. 선석 단위 정밀도를 포기하는 대신 착저 위험이 있는
배를 통과시키지 않는다 — "모르는 것을 안전으로 간주하지 않는다".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wharf",
        sa.Column(
            "depth_floored", sa.Boolean(), server_default=sa.text("false"),
            comment="웹이 얕은 선석을 누락해 min_water_depth_m 를 공공 API 값으로 "
                    "끌어내린 부두. True 면 이 부두의 선석별 수심은 신뢰할 수 없어 "
                    "감사가 부두 최저수심으로 판정한다",
        ),
    )


def downgrade() -> None:
    op.drop_column("wharf", "depth_floored")
