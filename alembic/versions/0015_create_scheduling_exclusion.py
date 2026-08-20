"""create scheduling_exclusion (자동추천 제외 건 — 관제 경고 센터 연동)

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-19

08_스케줄링_전면재설계_자동배정_설계문서.md §5.3 3번 — NO_ELIGIBLE_BERTH/
ALL_CANDIDATES_UNSAFE/WEATHER_BLOCKED로 자동배정이 안 된 건을 관제 경고 센터
(/dashboard/alerts)가 노출하기 위한 저장소. call_sign 유니크 — 배 1척당 최신
상태 1행만 유지하는 스냅샷 테이블이다(이력 아님, arrival_watcher.py가 매 주기
upsert/delete로 정리한다).
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduling_exclusion",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("call_sign", sa.String(), nullable=False, comment="선박 호출부호"),
        sa.Column("vessel_name", sa.String(), nullable=True),
        sa.Column("imo_no", sa.String(), nullable=True),
        sa.Column("cargo_chem_id", sa.String(), nullable=True, comment="msds_chemical.chem_id"),
        sa.Column(
            "decision", sa.String(), nullable=False,
            comment="NO_ELIGIBLE_BERTH/ALL_CANDIDATES_UNSAFE/WEATHER_BLOCKED",
        ),
        sa.Column("reason", sa.String(), nullable=True, comment="오케스트레이터 summary"),
        sa.Column("draught_m", sa.Float(), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="최초 발생 시각"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="가장 최근 재확인 시각"),
        sa.UniqueConstraint("call_sign", name="uq_scheduling_exclusion_call_sign"),
        sa.CheckConstraint(
            "decision IN ('NO_ELIGIBLE_BERTH', 'ALL_CANDIDATES_UNSAFE', 'WEATHER_BLOCKED')",
            name="ck_scheduling_exclusion_decision",
        ),
    )
    op.create_index("idx_scheduling_exclusion_decision", "scheduling_exclusion", ["decision"])


def downgrade() -> None:
    op.drop_table("scheduling_exclusion")
