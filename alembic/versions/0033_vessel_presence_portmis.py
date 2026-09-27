"""vessel presence portmis — 접안 인정 조건을 하루 늦은 VTS 이력에서 떼어 낸다

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-27

mart.vessel_presence 는 정지한 배를 "VTS 입출항 기록(upa_port_call)이 있을 때만"
부두 접안으로 인정했다. upa_port_call 은 하루 1번 수집이라 그 사이 입항·이선한
배가 판정 잡·게이트 인터락·이웃 점유에서 빠졌다. 조건을 "VTS 기록 또는 PORT-MIS
현재 입항 건(10분 주기)"으로 넓힌다. 근거와 실측은 ../sql/0033_vessel_presence_portmis.sql 머리 주석.

되돌리기: 0029 의 vessel_presence 정의를 그대로 떼어 둔 파일을 실행한다.
뷰뿐이라 잃는 데이터가 없다.
"""
from pathlib import Path
from typing import Sequence, Union

from sqlalchemy.util import await_only

from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _exec_sql_file(path: Path) -> None:
    # 0028·0029 와 같은 이유로 드라이버 연결에서 파일 전체를 한 번에 실행한다.
    raw = op.get_bind().connection.driver_connection
    await_only(raw.execute(path.read_text(encoding="utf-8")))


def upgrade() -> None:
    _exec_sql_file(_SQL_DIR / "0033_vessel_presence_portmis.sql")


def downgrade() -> None:
    _exec_sql_file(_SQL_DIR / "0033_vessel_presence_portmis_down.sql")
