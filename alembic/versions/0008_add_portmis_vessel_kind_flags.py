"""add portmis_vessel kind flags (liquid cargo barge / bunkering vessel)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-11

data-pipeline의 portmis_preprocessor.py가 선박종별코드(ship_kind_cd)로 액체화물
부선·급유선 여부를 파생해 채우는 컬럼. is_liquid_cargo_vessel(PORT-MIS 공식 신고)과
별개로 두는 이유는 mart_views.sql identity_confidence 설계 의도와 같다 — 소형
급유선·부선은 AIS Class B라 선종이 확인 안 되는 경우가 많아, "액체화물선 아님"으로
단정하면 실제로는 위험물을 옮겨싣는 배가 관제 화면에서 사라진다.

이 리비전은 alembic_version 드리프트(2026-08-11 정리)를 바로잡는 과정에서
새로 작성됐다 — 원본 마이그레이션 파일이 커밋되지 않은 채 DB에만 컬럼이
반영돼 있었다(라이브 DB 실측으로 컬럼 존재·데이터 144행 중 2행 확인 후 작성).
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "portmis_vessel",
        sa.Column("is_liquid_cargo_barge", sa.Boolean(), nullable=True, comment="액체화물 부선 여부"),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column("is_bunkering_vessel", sa.Boolean(), nullable=True, comment="급유선 여부"),
    )


def downgrade() -> None:
    op.drop_column("portmis_vessel", "is_bunkering_vessel")
    op.drop_column("portmis_vessel", "is_liquid_cargo_barge")
