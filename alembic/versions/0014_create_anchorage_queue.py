"""create anchorage_queue (정박지 대기열)

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-19

08_스케줄링_전면재설계_자동배정_설계문서.md §4.3 — berth_assignment와 분리된
정박지 대기열. REQUESTED가 이제 "선석 추천, 승인 대기"라는 구체적 의미를 가지므로
(§5.3), berth_id 없는 "정박지 대기"를 같은 상태값으로 표현하지 않는다. 정박지는
잠금 대상이 아니므로(§4.3) EXCLUDE 제약 같은 자원 잠금 장치는 두지 않는다.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "anchorage_queue",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("call_sign", sa.String(), nullable=True, comment="선박 호출부호"),
        sa.Column("vessel_name", sa.String(), nullable=True),
        sa.Column("imo_no", sa.String(), nullable=True),
        sa.Column("cargo_chem_id", sa.String(), nullable=True, comment="msds_chemical.chem_id"),
        sa.Column("cargo_cas_no", sa.String(), nullable=True),
        sa.Column("dwt_t", sa.Float(), nullable=True, comment="선박 DWT — 정박지 톤수 매칭에 사용"),
        sa.Column("draught_m", sa.Float(), nullable=True),
        sa.Column("anchorage_id", sa.String(), nullable=True, comment="Neo4j Anchorage.id"),
        sa.Column(
            "entered_at", sa.DateTime(timezone=True), nullable=False,
            comment="대기열 진입 시각 — anchorage_promoter FCFS 우선순위 기준(§5.4, §5.6)",
        ),
        sa.Column(
            "window_start", sa.DateTime(timezone=True), nullable=True,
            comment="원래 희망 접안 시작 시각 — 승격 시 resolve_berth_assignment 재검증에 재사용",
        ),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status", sa.String(), nullable=False, server_default="WAITING",
            comment="WAITING/PROMOTED/CANCELLED",
        ),
        sa.Column(
            "promoted_berth_assignment_id", sa.BigInteger(),
            sa.ForeignKey("berth_assignment.id", ondelete="SET NULL"), nullable=True,
            comment="승격 시 생성된 berth_assignment(status=REQUESTED) 행의 id — 감사 이력, 삭제 안 함",
        ),
        sa.Column("assignment_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('WAITING', 'PROMOTED', 'CANCELLED')", name="ck_anchorage_queue_status"
        ),
    )
    op.create_index("idx_anchorage_queue_status", "anchorage_queue", ["status"])
    op.create_index("idx_anchorage_queue_call_sign", "anchorage_queue", ["call_sign"])


def downgrade() -> None:
    op.drop_table("anchorage_queue")
