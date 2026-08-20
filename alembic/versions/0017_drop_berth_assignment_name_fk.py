"""drop berth_assignment -> upa_berth_facility(wharf_name) FK

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-20

0016 이 berth_assignment.berth_id 에 upa_berth_facility(wharf_name) 참조 FK 를
걸었는데, 그 대상 컬럼은 유일하지 않다. 이 마이그레이션이 나온 PC 에서는
FK 생성이 아예 실패했고(there is no unique constraint matching given keys),
먼저 적용한 PC 에는 FK 가 남아 있어 두 DB 가 갈라졌다.

원인은 데이터가 아니라 키 선택이다 — 'SK2부두' 는 SK가스㈜(LPG, 7.5m)와
SK에너지㈜(석유제품, 8.0m) 두 곳이 같은 이름을 쓰는 실제 사례다. 부두명은
이 도메인에서 키가 아니며(그래서 mart.facility_alias 대조 계층이 따로 있다),
유니크 제약을 걸면 실재하는 부두 하나를 지워야 한다.

FK 가 있으면 지우고, 없으면 아무 것도 하지 않는다(IF EXISTS) — 어느 PC 에서
실행해도 같은 상태로 수렴한다.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE berth_assignment "
        "DROP CONSTRAINT IF EXISTS berth_assignment_berth_id_fkey"
    )


def downgrade() -> None:
    # 되돌리지 않는다 — 대상 컬럼이 유일하지 않아 FK 를 다시 걸 수 없다.
    pass
