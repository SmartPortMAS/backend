"""berth_assignment: support REJECTED decisions

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-16

기존 스키마(0011)는 "예약이 성립된 경우"만 가정해 berth_id(FK)와 planned_window가
사실상 필수처럼 쓰였다. 반려는 선석 예약 자체가 없을 수 있으므로(예: 적합 선석
없음) 이 두 컬럼을 nullable로 두고, 상태값에 REJECTED를 추가한다. REJECTED는
ACTIVE_STATUSES에 넣지 않는다 — 0011의 EXCLUDE 제약이
`WHERE status IN (ACTIVE_STATUSES)`라 REJECTED 행은 애초에 겹침 검사 대상에서
빠지므로, planned_window가 NULL이어도 제약과 충돌하지 않는다.

승인자 식별용 approved_by 컬럼도 추가한다 — 로그인 체계가 없어 자유 텍스트로
관제사 이름을 받는다.

(재작성 메모, 2026-08-16 야간) 이 파일은 원래 존재했다가 소스가 삭제됐다.
`alembic/versions/__pycache__`에 남아있던 컴파일된 .pyc의 문자열 상수로부터
설계 취지를 복구했고, 정확한 컬럼·제약은 이미 이 스키마가 적용된 상태로
남아있던 실제 개발 DB(information_schema/pg_constraint)를 직접 조회해 그대로
옮겨적었다. berth_id/planned_window는 0011 생성 시점부터 이미 nullable로
잡혀 있었음을 실제 DB에서 확인했다 — 이 리비전은 CHECK 제약에 REJECTED를
추가하고 approved_by 컬럼을 더하는 것이 핵심 변경이다.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUSES_BEFORE = ("REQUESTED", "APPROVED", "SCHEDULED", "BERTHED", "COMPLETED", "CANCELLED")
_STATUSES_AFTER = _STATUSES_BEFORE + ("REJECTED",)


def upgrade() -> None:
    op.add_column(
        "berth_assignment",
        sa.Column("approved_by", sa.String(), nullable=True, comment="승인/반려한 관제사(자유 텍스트 — 로그인 체계 없음)"),
    )
    op.drop_constraint("ck_berth_assignment_status", "berth_assignment", type_="check")
    op.create_check_constraint(
        "ck_berth_assignment_status",
        "berth_assignment",
        "status IN (" + ", ".join(f"'{s}'" for s in _STATUSES_AFTER) + ")",
    )


def downgrade() -> None:
    op.drop_constraint("ck_berth_assignment_status", "berth_assignment", type_="check")
    op.create_check_constraint(
        "ck_berth_assignment_status",
        "berth_assignment",
        "status IN (" + ", ".join(f"'{s}'" for s in _STATUSES_BEFORE) + ")",
    )
    op.drop_column("berth_assignment", "approved_by")
