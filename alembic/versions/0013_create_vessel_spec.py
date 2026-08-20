"""create vessel_spec (선박제원정보, SicsVsslManp3/Info3)

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-19

08_스케줄링_전면재설계_자동배정_설계문서.md §5.2.1-A — 안벽 길이(berth.length_m) ↔
선박 전장(vsslTotLt) 게이트를 위해 신설. callsgn당 최신 1행(upsert) — 선박 제원은
자주 바뀌지 않으므로 이력이 아니라 최신 상태만 유지한다.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _common_metadata_columns() -> list[sa.Column]:
    return [
        sa.Column("source_system", sa.String(length=50), nullable=False),
        sa.Column("source_table", sa.String(length=50), nullable=False),
        sa.Column("collected_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_flag", sa.String(length=20), nullable=False, server_default="OK"),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
    ]


def upgrade() -> None:
    op.create_table(
        "vessel_spec",
        sa.Column("callsgn", sa.String(length=20), primary_key=True, comment="호출부호(조회 키)"),
        sa.Column("inout_port_se", sa.String(length=50), nullable=True, comment="내외항구분(ibobprt)"),
        sa.Column("vessel_no", sa.String(length=50), nullable=True, comment="선박번호"),
        sa.Column("imo_no", sa.String(length=20), nullable=True, comment="IMO번호"),
        sa.Column("vessel_kor_name", sa.String(length=200), nullable=True, comment="선박한글명"),
        sa.Column("vessel_eng_name", sa.String(length=200), nullable=True, comment="선박영문명"),
        sa.Column("vessel_kind", sa.String(length=100), nullable=True, comment="선박종류"),
        sa.Column("vessel_nationality", sa.String(length=100), nullable=True, comment="선박국적"),
        sa.Column("ton_edyc_se", sa.String(length=20), nullable=True, comment="톤수증서구분 코드"),
        sa.Column("ton_edyc_se_name", sa.String(length=100), nullable=True, comment="톤수증서구분명"),
        sa.Column("intrl_gross_tonnage", sa.Float(), nullable=True, comment="국제총톤수"),
        sa.Column("gross_tonnage", sa.Float(), nullable=True, comment="총톤수"),
        sa.Column("net_tonnage", sa.Float(), nullable=True, comment="순톤수"),
        sa.Column("loa_m", sa.Float(), nullable=True, comment="선박총길이(vsslTotLt) — 안벽길이 게이트 핵심 값"),
        sa.Column("beam_m", sa.Float(), nullable=True, comment="선박너비(shdth)"),
        sa.Column("draught_m", sa.Float(), nullable=True, comment="선박흘수(vsslDrft)"),
        sa.Column("registered_length_m", sa.Float(), nullable=True, comment="선박길이(vsslLt, 등록길이 계열)"),
        sa.Column("depth_m", sa.Float(), nullable=True, comment="선박깊이(vsslDp, molded depth)"),
        sa.Column("bareboat_charter_se", sa.String(length=20), nullable=True, comment="나용선구분 코드"),
        sa.Column("bareboat_charter_se_name", sa.String(length=100), nullable=True, comment="나용선구분명"),
        sa.Column("operation_shape_cd", sa.String(length=20), nullable=True, comment="운항형태 코드"),
        sa.Column("operation_shape_name", sa.String(length=100), nullable=True, comment="운항형태명"),
        sa.Column("built_at", sa.String(length=50), nullable=True, comment="선박건조일시(vsslCnstrDt, 원본 문자열 보존)"),
        sa.Column("prev_callsgn", sa.String(length=20), nullable=True, comment="이전호출부호(befClsgn)"),
        sa.Column("is_new_ship", sa.String(length=10), nullable=True, comment="신조선여부(nwshipAt, 원본 문자열 보존)"),
        *_common_metadata_columns(),
    )


def downgrade() -> None:
    op.drop_table("vessel_spec")
