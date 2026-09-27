"""ukc rule — 대시보드 흘수 점검의 필요 여유를 판정 잡과 같은 규칙 max(1.0m, 흘수 10%)로

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-27

mart.berth_draught_check · mart.berth_audit 의 필요 여유(UKC)를 흘수 10% 에서
max(1.0m, 흘수 10%) 로 바꾼다. 판정 잡(app/agents/scheduling/ukc.py)과 같은 식이다.
근거와 실측은 ../sql/0034_ukc_rule.sql 머리 주석.

되돌리기: 0029 의 두 뷰 정의(흘수 10%)를 다시 만든다. 뷰뿐이라 잃는 데이터가 없다.
"""
from pathlib import Path
from typing import Sequence, Union

from sqlalchemy.util import await_only

from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _exec_sql_file(path: Path) -> None:
    # 0028·0029 와 같은 이유로 드라이버 연결에서 파일 전체를 한 번에 실행한다.
    raw = op.get_bind().connection.driver_connection
    await_only(raw.execute(path.read_text(encoding="utf-8")))


def upgrade() -> None:
    _exec_sql_file(_SQL_DIR / "0034_ukc_rule.sql")


def downgrade() -> None:
    _exec_sql_file(_SQL_DIR / "0034_ukc_rule_down.sql")
