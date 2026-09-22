"""(2026-09-22 무효화 — 0028 참고) upa_vessel_position 을 선박당 최신 1행으로

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-13

왜 바꾸나
--------
자연키가 (vessel_uid, received_at_utc) 여서 관측 시각마다 새 행이 쌓였다.
그런데 이 테이블의 이력을 읽는 코드가 하나도 없다 — 소비처(mart.vessel_latest_
position, dashboard_current)는 전부 DISTINCT ON 으로 최신 1행만 꺼내 쓴다.
이력 축적은 설계상 물리 마트(ulsan_vessel_mart)가 맡는다(mart_views.sql 헤더).

실측(2026-09-13):
  · 신호 갱신 주기 중앙 35분 → 10분 폴링해도 대부분 새 행이 생긴다
  · 34분 간격 두 번 수집에 569행 → 872행 (+303)
  · 그대로 두면 하루 약 23,000행, 1년 800만 행이 쌓인다

접안 판정에 이력이 필요하지 않은 근거
----------------------------------
  접안 = nav_status_code='정박(계류)' AND 최근접 선석 400m 이내

  · 상태 분리가 깨끗하다 — 계류는 선석까지 중앙 263m(400m 이내 71%),
    앵커링은 중앙 4,631m(400m 이내 8%). 거리로 20배 갈린다.
  · "N분 지속" 조건이 막으려던 상태 깜빡임이 실제로는 1.3%(4/297척)뿐이다.
  · 이선(移船)해도 좌표가 바뀌므로 최근접 선석 계산이 새 선석을 가리킨다.
    상태 타이머 한 개로는 못 잡는 경우를 거리 계산이 자동으로 처리한다.
  · nav_status_code 결측 30% 는 판정에 영향이 적다 — 결측 선박은 선석까지
    중앙 5,081m, 평균 SOG 2.77kt 로 대부분 먼바다 이동 중이다.

키를 MMSI 로 두는 이유
--------------------
vessel_uid = COALESCE(mmsi::text, 'CS:'||callsgn) 인데 실측 872행 전부
vessel_uid_source='MMSI' 다(MMSI 결측 0%). callsgn 은 쓸 수 없다:
  · 결측 26%(145/563) — NULL 은 유니크 인덱스에서 서로 다른 값이라 ON CONFLICT
    가 걸리지 않고 폴링마다 중복이 쌓인다(2026-08 에 겪고 vessel_uid 로 전환).
  · 쓰레기값이 섞인다 — 한 응답 안에서 '301' 이 4척(KCG D-01 / YEOUNG SUNG HO /
    TEAPYUNGYANG HO / RIVERCRUISGE1), '500' 이 3척. 키로 쓰면 서로 다른 배가
    한 행으로 합쳐져 위치가 덮어써진다.

되돌리기
-------
downgrade 로 복합키를 되살리면 그 시점부터 다시 쌓인다. 다만 지나간 이력은
복원되지 않는다 — 이 마이그레이션은 그 점을 감수한 선택이다.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_IDX = "upa_vessel_position_uidx__vessel_uid__received_at_utc"
_NEW_IDX = "upa_vessel_position_uidx__vessel_uid"


def upgrade() -> None:
    # [2026-09-22 되돌림] 이 리비전은 이제 아무것도 지우지 않는다.
    #   원래는 선박당 최신 1행만 남기고(DELETE) 키를 vessel_uid 단독으로 바꿨다.
    #   그런데 "이력은 ulsan_vessel_mart 가 맡는다"던 그 표를 0027 이 지워, 둘을
    #   함께 돌리면 위치 이력이 어디에도 남지 않는다(실측: 6/27~ 93,404행 -> 2,688행).
    #   아직 0020 을 안 돌린 DB 는 이력을 그대로 지키고, 이미 돌린 DB 는 0028 이
    #   키를 되살린다(지워진 행은 S3 시각별 원본으로 8/6 이후를 다시 채울 수 있다).
    pass


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_NEW_IDX}")
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_OLD_IDX} "
        f"ON upa_vessel_position (vessel_uid, received_at_utc)"
    )
