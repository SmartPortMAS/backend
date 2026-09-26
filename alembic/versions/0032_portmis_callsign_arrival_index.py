"""portmis callsign arrival index — vessel_current_call 의 배별 최근 입항 건 조회 인덱스

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-26

왜
--
mart.vessel_current_call(0030)은 배마다 "가장 최근 PORT-MIS 입항 건"을 LATERAL 로
찾는다(WHERE upper(btrim(p.callsgn)) = v.callsgn ORDER BY p.arrival_at_utc DESC LIMIT 1).
비교가 식이라 기존 자연키 인덱스(callsgn, entry_year, entry_count)를 못 쓰고 매번
portmis_vessel 을 통째로 훑었다.

mart.berth_current_cargo 에서는 이 뷰가 접안 선박 수만큼 다시 계산돼(플래너가 접안
선박을 1척으로 추정 → Nested Loop) 곱으로 커졌다. 2026-09-26 로컬 DB EXPLAIN ANALYZE:
  접안 62척 × 알려진 선박 1,113척 = portmis 전체 스캔 69,006회 × 1.8ms = 122초 / 총 124초.
backend arrival_watcher 가 배마다 이 뷰를 읽어 한 척 판정에 2분씩 걸렸다.

무엇을
------
LATERAL 의 비교식·정렬과 똑같은 식 인덱스를 만든다. 식이 한 글자라도 다르면 플래너가
쓰지 않는다 — 뷰는 upper(btrim((callsgn)::text)) 로 비교한다.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_portmis_vessel_callsign_arrival "
        "ON public.portmis_vessel USING btree (upper(btrim((callsgn)::text)), arrival_at_utc DESC)"
    )
    op.execute("ANALYZE public.portmis_vessel")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_portmis_vessel_callsign_arrival")
