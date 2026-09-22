"""drop assignment-era and dead tables

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-21

표 5개를 지운다. 전부 **지금 0행이거나 읽는 곳이 없는** 표다.

┌──────────────────────┬──────┬────────────────────────────────────────────────┐
│ 표                   │ 행수 │ 지우는 근거                                    │
├──────────────────────┼──────┼────────────────────────────────────────────────┤
│ berth_assignment     │    0 │ 우리가 만들던 선석 **예약**. 방향 C 에서 우리는 │
│                      │      │ 배정하지 않는다 → 0026 assessment_history 가    │
│                      │      │ 대체한다("예약"이 아니라 "판정")               │
│ anchorage_queue      │    0 │ 정박지 **대기열**. 승격(빈 슬롯 → 배정)을 전제  │
│                      │      │ 로 한 표인데 그 승격을 없앴다                   │
│                      │      │ (app/jobs/anchorage_promoter.py 삭제)           │
│ scheduling_exclusion │    0 │ "자동 배정이 배정을 못 만든 건"의 목록.         │
│                      │      │ 배정을 안 하므로 못 만든 건도 없다 →            │
│                      │      │ assessment_history 의 부적합·판정불가가 대체    │
│ ais_vessel_position  │    0 │ 레거시 AIS. UPA 선박위치(upa_vessel_position)가 │
│                      │      │ 대체한 지 오래고, 짝이던 ais_vessel_static 은   │
│                      │      │ 이미 0009 가 지웠다. 9/17 회의 "AIS 테이블      │
│                      │      │ 제거 ✅ 찬성"                                   │
│ ulsan_vessel_mart    │  964 │ 83컬럼 물리 마트. **읽는 곳 0건** — 백엔드 참조 │
│                      │      │ 없음, mart 스키마 뷰 의존 없음(pg_depend 확인). │
│                      │      │ 같은 결합을 mart.dashboard_current 등 뷰가 이미 │
│                      │      │ 한다. 생산 단계도 함께 제거                     │
│                      │      │ (run_pipeline.run_mart)                         │
└──────────────────────┴──────┴────────────────────────────────────────────────┘

`ulsan_vessel_mart` 는 backend(Alembic) 소유가 아니라 data-pipeline 이 auto_create
로 만들던 표다. 소유권상 여기서 지우는 게 어색하지만, 지우는 시점을 한곳에 모아
두는 편이 추적에 낫다고 봤다. `IF EXISTS` 로 지우고 downgrade 에서 되살리지 않는다
— 파생 표라 CSV 에서 언제든 재생성된다.

되돌리기: downgrade 는 앞의 네 표를 **빈 껍데기로만** 되살린다. 행 데이터는
복원하지 않는다(애초에 0행이었다). 제약·인덱스는 원본과 같게 만든다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 순서: 참조하는 쪽부터. anchorage_queue.promoted_berth_assignment_id 가
    # berth_assignment 를 가리키므로 먼저 지운다.
    op.execute("DROP TABLE IF EXISTS anchorage_queue CASCADE")
    op.execute("DROP TABLE IF EXISTS berth_assignment CASCADE")
    op.execute("DROP TABLE IF EXISTS scheduling_exclusion CASCADE")
    op.execute("DROP TABLE IF EXISTS ais_vessel_position CASCADE")
    # data-pipeline 소유(auto_create). 생산 단계는 run_pipeline.run_mart 에서 제거했다.
    op.execute("DROP TABLE IF EXISTS ulsan_vessel_mart CASCADE")


def downgrade() -> None:
    """빈 껍데기 복원. 행 데이터는 되살리지 않는다(원래 0행)."""
    op.create_table(
        "berth_assignment",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("berth_id", sa.String(), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("call_sign", sa.String(), nullable=True),
        sa.Column("imo_no", sa.String(), nullable=True),
        sa.Column("vessel_name", sa.String(), nullable=True),
        sa.Column("cargo_chem_id", sa.String(), nullable=True),
        sa.Column("planned_window", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="REQUESTED"),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("assignment_reason", sa.Text(), nullable=True),
        sa.Column("rejected_candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("actual_berthing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_departure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 0025 가 좁혀 둔 상태 집합 그대로 되살린다(REQUESTED 는 자원을 잠그지 않는다).
    op.execute(
        "ALTER TABLE berth_assignment ADD CONSTRAINT berth_assignment_no_overlap "
        "EXCLUDE USING gist (berth_id WITH =, slot_no WITH =, planned_window WITH &&) "
        "WHERE (status IN ('APPROVED', 'SCHEDULED', 'BERTHED'))"
    )

    op.create_table(
        "anchorage_queue",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("call_sign", sa.String(), nullable=True),
        sa.Column("imo_no", sa.String(), nullable=True),
        sa.Column("vessel_name", sa.String(), nullable=True),
        sa.Column("cargo_chem_id", sa.String(), nullable=True),
        sa.Column("dwt_t", sa.Float(), nullable=True),
        sa.Column("draught_m", sa.Float(), nullable=True),
        sa.Column("anchorage_id", sa.String(), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="WAITING"),
        sa.Column("assignment_reason", sa.Text(), nullable=True),
        sa.Column("promoted_berth_assignment_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "scheduling_exclusion",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("call_sign", sa.String(), nullable=False, unique=True),
        sa.Column("vessel_name", sa.String(), nullable=True),
        sa.Column("imo_no", sa.String(), nullable=True),
        sa.Column("cargo_chem_id", sa.String(), nullable=True),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("draught_m", sa.Float(), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "ais_vessel_position",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("mmsi", sa.String(length=20), nullable=False),
        sa.Column("received_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("sog", sa.Float(), nullable=True),
        sa.Column("cog", sa.Float(), nullable=True),
        sa.Column("heading", sa.Float(), nullable=True),
        sa.Column("nav_status_code", sa.String(length=50), nullable=True),
        sa.Column("source_system", sa.String(length=50), nullable=False),
        sa.Column("source_table", sa.String(length=50), nullable=False),
        sa.Column("collected_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_flag", sa.String(length=20), nullable=False, server_default="OK"),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # ulsan_vessel_mart 는 파생 표라 복원하지 않는다 — CSV 에서 재생성한다.
