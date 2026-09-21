"""create assessment_history (D1)

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-21

9/17 회의 §8 이 정한 판정 이력 테이블. 회의록·PPT 20번 부록은 이 마이그레이션을
**0019** 라고 적었는데, 그 번호는 그 사이 `0019_add_portmis_facility_subcode_and_cargo_ton`
이 가져갔다(head 는 0025). 그래서 **0026** 이다.

회의 초안에서 바꾼 것은 세 가지뿐이다 — 이유는 app/models/assessment_history.py 주석.
  1. level 을 기존 RiskLevel 과 분리한다 (RiskLevel 의 '배정불가'는 배정 주체의 어휘)
  2. input_snapshot 에 portmis_collected_at 을 담기로 한다 (PORT-MIS 동결 대응)
  3. stage 전이는 AIS 항해상태로만 정한다 (같은 이유)

이 표는 `berth_assignment`(우리가 만들던 예약)를 대체한다 — 0027 이 그 표를 지운다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assessment_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("call_sign", sa.String(), nullable=False),
        sa.Column("vessel_name", sa.String(), nullable=True),
        sa.Column("stage", sa.String(length=20), nullable=False, comment="입항전|접안직전|하역중"),
        sa.Column(
            "wharf_name", sa.String(), nullable=True,
            comment="판정 대상 계류시설(정규화 후). 정박지 배정·미해소 표기면 NULL",
        ),
        sa.Column("level", sa.String(length=20), nullable=False, comment="적합|주의|부적합|판정불가"),
        sa.Column(
            "axes", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment='축별 등급·근거 {"기상": {...}, "흘수": {...}, "혼재": {...}, "점유": {...}}',
        ),
        sa.Column(
            "reasons", postgresql.ARRAY(sa.Text()), nullable=False,
            server_default=sa.text("'{}'::text[]"), comment="사람이 읽는 근거 문장",
        ),
        sa.Column("action", sa.String(length=20), nullable=True, comment="대체선석|정박지대기|입항보류|하역보류"),
        sa.Column("action_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recipient", sa.String(length=20), nullable=True, comment="선석운영주체|VTS|터미널"),
        sa.Column("graph_path", postgresql.ARRAY(sa.Text()), nullable=True, comment="D3 근거 경로 문장"),
        sa.Column(
            "changed_from", sa.String(length=20), nullable=True,
            comment="같은 선박·같은 stage 직전 판정의 level. 변화가 없으면 애초에 기록하지 않는다",
        ),
        sa.Column(
            "input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True,
            comment=(
                "판정에 쓴 관측의 시각과 값(재현용). portmis_collected_at 을 반드시 담는다 — "
                "PORT-MIS 는 수집창 밖이면 동결되므로, 이 값이 없으면 '며칠 묵은 배정으로 "
                "내려진 판정인가'를 사후에 가릴 방법이 없다"
            ),
        ),
        sa.Column(
            "assessed_at_utc", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("acknowledged_by", sa.String(), nullable=True),
        sa.Column("acknowledged_at_utc", sa.DateTime(timezone=True), nullable=True),
        comment="판정 이력(D1). 배정이 아니라 '지금 자리가 조건에 맞는가'의 기록.",
    )
    op.create_index("ix_assessment_history_call_sign", "assessment_history", ["call_sign"])
    op.create_index(
        "ix_assessment_history_call_sign_time", "assessment_history", ["call_sign", "assessed_at_utc"],
    )
    # 게이트(/ws/hardware)가 매 갱신마다 던지는 질의 — "이 선석의 최신 하역중 판정".
    op.create_index(
        "ix_assessment_history_wharf_time", "assessment_history", ["wharf_name", "assessed_at_utc"],
    )


def downgrade() -> None:
    op.drop_index("ix_assessment_history_wharf_time", table_name="assessment_history")
    op.drop_index("ix_assessment_history_call_sign_time", table_name="assessment_history")
    op.drop_index("ix_assessment_history_call_sign", table_name="assessment_history")
    op.drop_table("assessment_history")
