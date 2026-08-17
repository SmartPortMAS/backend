"""drop ais_vessel_static (legacy AIS static table, superseded by UPA position feed)

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-16

ais_vessel_static은 aisstream 웹소켓 기반 레거시 AIS 정적 정보 테이블이다.
현재 실시간 위치의 기본 소스는 UPA getVslPstnInfo(callsgn/mmsi/imo_no/vessel_name/
draught를 매 행 native로 포함)로 전환됐고, 선종/액체화물 판정은 PORT-MIS
(portmis_vessel)가 정본이다 — ais_vessel_static을 조인하는 mart 뷰·백엔드
엔드포인트·에이전트 서비스가 없음을 확인했다(2026-08-16 조사).

length/width(LOA/beam)만 이 테이블에만 있던 값인데, ship_type이 전량 NaN으로
들어오는 등 품질 문제로 이미 사용이 중단된 상태였다 — 삭제로 인해 잃는
실사용 기능은 없다. 이후 선박 제원이 필요해지면 신뢰 가능한 별도 소스를
새로 확보해야 한다(이 테이블을 되살리는 것은 해법이 아님).
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("ais_vessel_static")


def downgrade() -> None:
    op.create_table(
        "ais_vessel_static",
        sa.Column("mmsi", sa.Integer(), primary_key=True),
        sa.Column("imo_no", sa.String(length=20), nullable=True),
        sa.Column("callsgn", sa.String(length=20), nullable=True),
        sa.Column("vessel_name", sa.String(length=200), nullable=True),
        sa.Column("ship_type", sa.String(length=10), nullable=True),
        sa.Column("length", sa.Float(), nullable=True),
        sa.Column("width", sa.Float(), nullable=True),
        sa.Column("draught", sa.Float(), nullable=True),
        sa.Column("Destination", sa.String(length=200), nullable=True),
        sa.Column("ulsan_bound", sa.Boolean(), nullable=True),
        sa.Column("is_liquid_cargo_vessel", sa.Boolean(), nullable=True),
        sa.Column("source_system", sa.String(length=50), nullable=False),
        sa.Column("source_table", sa.String(length=50), nullable=False),
        sa.Column("collected_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_flag", sa.String(length=20), nullable=False, server_default="OK"),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
