"""create mart schema views — data-pipeline/mart_views.sql 을 Alembic 으로 이관

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-24

mart 스키마(뷰 20 · 구체화 뷰 mart.facility_alias · 함수 4)는 backend 가 읽는 조회
계층인데, 지금까지 data-pipeline/mart_views.sql 을 사람이 DBeaver/psql 로 돌려서
만들었다. 그래서 새 DB 에는 뷰가 없고, 어떤 DB 가 어느 버전의 뷰를 가졌는지 기록도
없었다. 0028 과 같은 결정(스키마는 전부 Alembic)의 두 번째 절반이다.

../sql/0029_mart_views.sql 은 data-pipeline origin/dev(deb08f8)의 mart_views.sql 고정본이다.
파일이 앞에서 DROP VIEW IF EXISTS 로 지우고 다시 만드므로:
  · 빈 DB  : 새로 만든다.
  · 로컬 DB: 같은 정의로 다시 만든다(뷰는 데이터를 들지 않는다). facility_alias 는
             구체화 뷰라 다시 만들 때 그 시점 데이터로 채워진다 — REFRESH 와 같다.

앞으로 뷰를 바꿀 때는 이 파일이 아니라 새 리비전에서 CREATE OR REPLACE VIEW 를 쓴다.
data-pipeline 의 mart 도메인은 계속 REFRESH MATERIALIZED VIEW mart.facility_alias 만 한다
(데이터 갱신이지 구조 변경이 아니다).

되돌리기: mart 스키마를 통째로 지운다. 뷰·함수뿐이라 잃는 데이터가 없다.
"""
from pathlib import Path
from typing import Sequence, Union

from sqlalchemy.util import await_only

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL = Path(__file__).resolve().parent.parent / "sql" / "0029_mart_views.sql"


def _exec_sql_file(path: Path) -> None:
    # 0028 과 같은 이유로 드라이버 연결에서 파일 전체를 한 번에 실행한다.
    raw = op.get_bind().connection.driver_connection
    await_only(raw.execute(path.read_text(encoding="utf-8")))


def upgrade() -> None:
    _exec_sql_file(_SQL)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS mart CASCADE")
