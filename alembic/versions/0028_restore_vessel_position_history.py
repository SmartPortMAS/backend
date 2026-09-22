"""upa_vessel_position 이력 키 복구 — (vessel_uid, received_at_utc)

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-22

0020(선박당 최신 1행)을 무효화했다. 이 리비전은 0020 을 이미 돌린 DB 에서
유니크 키를 (vessel_uid, received_at_utc) 로 되돌려, 그 시점부터 이력이 다시 쌓이게
한다. 0020 을 안 돌린 DB 에서는 이미 그 키라 아무 일도 하지 않는다.

지워진 이력은 이 리비전으로 돌아오지 않는다. EC2 가 매시 S3 에 남긴 원본
(raw/upa/upa_vessel_position_YYYYMMDDTHHZ_raw.json, 2026-08-06~)을
data-pipeline 의 cloud_pull.py 가 재생해 다시 채운다.

왜 이력이 필요한가
  · 백테스트(checks/backtest_false_alarm.py)가 과거 흘수를 읽는다
  · 입항·접안·이안 시각 검증, 사례 재생(Omniverse 정밀 검토)
  · 최신 위치 소비처는 모두 스스로 최신 1행을 고르므로 영향이 없다
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HISTORY_IDX = "upa_vessel_position_uidx__vessel_uid__received_at_utc"
_LATEST_ONLY_IDX = "upa_vessel_position_uidx__vessel_uid"


def upgrade() -> None:
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_HISTORY_IDX} "
        f"ON upa_vessel_position (vessel_uid, received_at_utc)"
    )
    op.execute(f"DROP INDEX IF EXISTS {_LATEST_ONLY_IDX}")


def downgrade() -> None:
    # 되돌리려면 선박당 1행으로 지워야 한다 — 이력을 지우는 되돌리기는 두지 않는다.
    pass
