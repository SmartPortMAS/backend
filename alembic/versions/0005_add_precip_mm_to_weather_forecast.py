"""add precip_mm to weather_forecast

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-25

강수량(precip_mm) 참고 컬럼 추가. 기상청 getVilageFcst의 PCP 필드는 원래도
raw 응답에 있었지만 구간 텍스트("1mm 미만", "16.0mm", "강수없음" 등 혼재)라
data-pipeline이 지금까지 저장하지 않았다. weather_forecast_preprocessor.py의
normalize_pcp_mm()이 이를 mm 숫자로 근사 정규화해 채운다.

rule_engine 임계값 판정에는 아직 쓰지 않는다 — 항만 하역 중단 기준으로 흔히
인용되는 강수량 수치(시간당 1mm/20mm/30mm 등)가 원문 대조 없이 확인되지 않아,
값만 참고용으로 먼저 들여오고 임계값 도입은 근거가 검증된 뒤로 미룬다.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("weather_forecast", sa.Column("precip_mm", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("weather_forecast", "precip_mm")
