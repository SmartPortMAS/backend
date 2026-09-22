"""create tide_forecast table

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-20

조위 **예보**(고조/저조 극값) 저장소. `tide_obs`는 실측 전용이라 과거만 있고,
판정에 필요한 "체류 중 최저조"는 미래 구간이라 실측으로 잴 수 없다.

출처: 공공데이터포털 국립해양조사원 조석예보
      apis.data.go.kr/1192136/tideFcstHghLw/GetTideFcstHghLwApiService

하루 4건(고조 2·저조 2)의 극값만 주는 API다. 연속 곡선이 아니므로 임의 시각의
조위는 소비 측에서 극값 사이를 보간해 쓴다(반일주조 근사).

extr_type은 API의 extrSe를 그대로 옮긴 값이다. 명세 문서(HWP)를 구할 수 없어
2026-09-20 ~ 10-01 울산(DT_0020) 응답 45개 극값을 시간순 이웃과 대조해 확정했다
— 1·3은 전부 극대, 2·4는 전부 극소로 예외가 없었다.
  1 = 제1고조 · 2 = 제1저조 · 3 = 제2고조 · 4 = 제2저조
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tide_forecast",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("station_id", sa.String(length=20), nullable=True),
        sa.Column("station_name", sa.String(length=100), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        # 예보 시각(UTC). tide_obs.observed_at_utc 와 대응한다.
        sa.Column("predicted_at_utc", sa.DateTime(timezone=True), nullable=True),
        # 조위(cm, 기본수준면 기준) — tide_obs.tide_level_cm 과 같은 단위·기준면
        sa.Column("tide_level_cm", sa.Float(), nullable=True),
        # 1=제1고조 2=제1저조 3=제2고조 4=제2저조
        sa.Column("extr_type", sa.String(length=4), nullable=True),
        # 고조/저조 (extr_type 에서 파생 — 소비 측이 코드값을 몰라도 되게)
        sa.Column("extr_kind", sa.String(length=8), nullable=True),
        sa.Column("source_system", sa.String(length=50), nullable=False),
        sa.Column("source_table", sa.String(length=50), nullable=False),
        sa.Column("collected_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_flag", sa.String(length=20), nullable=False, server_default="OK"),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "idx_tide_forecast_natural_key",
        "tide_forecast",
        ["station_id", "predicted_at_utc"],
        unique=True,
    )
    # "체류 구간의 최저조"는 시각 범위 + 저조 필터로 긁는다.
    op.create_index(
        "idx_tide_forecast_window", "tide_forecast", ["predicted_at_utc", "extr_kind"],
    )


def downgrade() -> None:
    op.drop_index("idx_tide_forecast_window", table_name="tide_forecast")
    op.drop_index("idx_tide_forecast_natural_key", table_name="tide_forecast")
    op.drop_table("tide_forecast")
