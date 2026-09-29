"""current call — AIS 입항횟수보다 PORT-MIS 입항 건이 새로우면 PORT-MIS 를 따른다

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-29

mart.vessel_current_call 의 입항 건 규칙 보정. AIS 정적 정보(입항연도·횟수)가 며칠씩 갱신되지
않아, 새로 입항한 배를 지난 입항 건의 화물로 판정하고 있었다(실측 다인3호 #20 ↔ #21).
근거와 실측은 ../sql/0035_current_call_portmis_newer.sql 머리 주석.

되돌리기: 0030 의 뷰 정의를 다시 만든다. 뷰뿐이라 잃는 데이터가 없다.
"""
from pathlib import Path
from typing import Sequence, Union

from sqlalchemy.util import await_only

from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _exec_sql_file(path: Path) -> None:
    # 0028·0029 와 같은 이유로 드라이버 연결에서 파일 전체를 한 번에 실행한다.
    raw = op.get_bind().connection.driver_connection
    await_only(raw.execute(path.read_text(encoding="utf-8")))


def upgrade() -> None:
    _exec_sql_file(_SQL_DIR / "0035_current_call_portmis_newer.sql")


def downgrade() -> None:
    _exec_sql_file(_SQL_DIR / "0035_current_call_portmis_newer_down.sql")
