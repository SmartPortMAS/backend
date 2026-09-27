"""cargo by port call — 화물을 콜사인이 아니라 입항 건으로 잇는다

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-25

data-pipeline gen_cargo_manifest v4 가 합성 화물을 PORT-MIS 입항 건
(callsgn, entry_year, entry_count) 단위로 만들기 시작했다. 소비 뷰가 콜사인으로만
조인하면 30일 안에 다시 들어온 배의 지난 항차 화물이 섞인다(2026-09-25 로컬 DB:
액체선 290척이 396번 입항 — 약 100척이 재입항).

../sql/0030_cargo_by_port_call.sql:
  · mart.cargo_msds 끝에 entry_year · entry_count · port_call_key 추가
  · mart.vessel_current_call 신설 — 배마다 지금의 입항 건(규칙·채점은 SQL 머리 주석)
  · berth_current_cargo · dashboard_current · approval_candidates 를 입항 건 조인으로

되돌리기: 0029 의 뷰 정의 파일을 다시 실행해 뷰를 원래대로 만든 뒤
mart.vessel_current_call 을 지운다. 뷰뿐이라 잃는 데이터가 없다.
"""
from pathlib import Path
from typing import Sequence, Union

from sqlalchemy.util import await_only

from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _exec_sql_file(path: Path) -> None:
    # 0028·0029 와 같은 이유로 드라이버 연결에서 파일 전체를 한 번에 실행한다.
    raw = op.get_bind().connection.driver_connection
    await_only(raw.execute(path.read_text(encoding="utf-8")))


def upgrade() -> None:
    _exec_sql_file(_SQL_DIR / "0030_cargo_by_port_call.sql")


def downgrade() -> None:
    # 0029 파일은 의존 뷰부터 DROP VIEW IF EXISTS 로 지우고 다시 만든다.
    # vessel_current_call 은 그 목록에 없으므로 먼저 그것에 기대는 뷰를 0029 가
    # 다시 만들게 한 뒤(그때 의존이 풀린다) 지운다.
    _exec_sql_file(_SQL_DIR / "0029_mart_views.sql")
    op.execute("DROP VIEW IF EXISTS mart.vessel_current_call")
