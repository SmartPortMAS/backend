"""create berth_weather_threshold table

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-25

온산 MVP(feature/onsan-mvp, berth_weather_thresholds.csv)가 제안한 "부두그룹별 기상
임계값 + 중단/이안/호스분리 3단계 에스컬레이션"을 기존 기상분석 에이전트에 이식하기
위한 테이블. 스키마 소유권은 backend(Alembic)에 있고, 값 자체는 data-pipeline의
1회성 시드 로더(loaders/berth_weather_threshold_pg_loader.py)가 insert한다
(공통 컨벤션 — WORK_SUMMARY_2026-07-03 스키마 소유권 표 참고).

berth_group='__GLOBAL_DEFAULT__' 행은 특정 선석을 아직 모르는 호출(berth_group
미지정)을 위한 폴백 임계값이다. 기존 전역 상수(풍속 14m/s, 파고 1.5m)를 그대로
옮겨, berth_group 도입 이전 호출부와의 하위 호환을 보장한다.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "berth_weather_threshold",
        sa.Column("berth_group", sa.String(length=100), primary_key=True),
        sa.Column("operator", sa.String(length=100), nullable=True),
        sa.Column("stop_wind_ms", sa.Float(), nullable=True),
        sa.Column("stop_wave_m", sa.Float(), nullable=True),
        sa.Column("unberth_wind_ms", sa.Float(), nullable=True),
        sa.Column("unberth_wave_m", sa.Float(), nullable=True),
        sa.Column("disconnect_wind_ms", sa.Float(), nullable=True),
        sa.Column("disconnect_wave_m", sa.Float(), nullable=True),
        sa.Column("extra_conditions", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("collected_at_utc", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("berth_weather_threshold")
