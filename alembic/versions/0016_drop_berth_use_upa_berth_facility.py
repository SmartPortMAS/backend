"""drop berth table, point berth_assignment at upa_berth_facility

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-19

`berth`(backend Alembic 소유)와 `upa_berth_facility`(data-pipeline 소유, UPA GIS
항만시설정보 원본)가 사실상 같은 정보를 담은 두 테이블이었다 — `berth`의 모든
컬럼이 `upa_berth_facility`에서 그대로 파생 가능했고(berth_pg_loader.py가 매
파이프라인 사이클마다 동기화), 그 동기화 자체가 SK2부두 중복 행을 만드는 등
드리프트 버그의 원인이었다(2026-08-19 실측).

`berth`를 따로 둔 유일한 이유는 `berth_assignment.berth_id`가 참조무결성을
가질 안정된 FK 대상이 필요했기 때문인데, `upa_berth_facility`도 매 사이클
upsert-only(삭제 없음)라 같은 안정성을 제공한다 — `wharf_name`에 UNIQUE 인덱스가
있어 FK 대상으로 바로 쓸 수 있다. 그래서 중간 테이블을 없애고 직접 참조한다.

berth_assignment는 이 시점에 데모/테스트 배정 4건뿐이라 비우고 진행한다.

SQLAlchemy ORM은 `ForeignKey("berth.id")`를 같은 프로세스의 메타데이터에서
해석하므로, Python 모델 쪽 `Berth` 클래스도 함께 제거하고 `BerthAssignment.berth_id`는
(다른 UPA raw 테이블들과 같은 패턴으로) ORM 레벨 FK 선언 없이 plain String으로
바꾼다 — DB 레벨 FK 제약은 이 마이그레이션이 raw DDL로 건다.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # anchorage_queue.promoted_berth_assignment_id가 berth_assignment.id를 FK로
    # 참조해서(ON DELETE SET NULL) TRUNCATE는 못 쓴다(Postgres가 참조된 테이블의
    # TRUNCATE 자체를 거부함) — DELETE는 그 FK 액션을 행 단위로 정상 적용한다.
    op.execute("DELETE FROM berth_assignment")
    op.drop_constraint("berth_assignment_berth_id_fkey", "berth_assignment", type_="foreignkey")
    # 여기서 upa_berth_facility(wharf_name) 로 FK 를 다시 걸려 했으나 걸 수 없다.
    #
    # 이 테이블의 유니크 키는 record_uid 하나이고 wharf_name 에는 유니크 인덱스가
    # 없다. 그리고 만들 수도 없다 — 'SK2부두' 가 서로 다른 실제 부두 두 곳이기
    # 때문이다(2026-08-20 실측):
    #     SK2부두 · SK가스㈜   · 수심 7.5m · 길이 150m  (LPG 터미널)
    #     SK2부두 · SK에너지㈜ · 수심 8.0m · 길이 430m  (석유제품 부두)
    # 원천이 실제로 같은 이름을 쓰는 것이라 동기화 드리프트가 아니다. 우리가
    # 가스 카테고리 보정을 (선석명, 운영사) 키로 잡은 것도 같은 이유다.
    #
    # 이름은 이 도메인에서 키가 아니다(표기 흔들림 때문에 mart.facility_alias 라는
    # 대조 계층을 따로 두고 있다). 그래서 berth_id 는 FK 없는 문자열로 두고,
    # 참조 정합성은 배정 시점에 애플리케이션이 확인한다.
    op.drop_table("berth")


def downgrade() -> None:
    # upa_berth_facility 기반 배정 데이터는 복구하지 않는다(업그레이드 시 이미 비움).
    # berth 테이블 구조만 0010 정의대로 복원한다.
    op.create_table(
        "berth",
        sa.Column("id", sa.String(), primary_key=True, comment="선석 고유 ID(Neo4j Berth.id와 동일 값)"),
        sa.Column("wharf_name", sa.String(), nullable=False, comment="부두명"),
        sa.Column("port_name", sa.String(), nullable=True, comment="항만명(울산본항/온산항 등)"),
        sa.Column("length_m", sa.Float(), nullable=True, comment="안벽 길이(m)"),
        sa.Column(
            "depth_m", sa.Float(), nullable=True,
            comment="수심(m, 해도기준면 Chart Datum 기준 — 실제 가용수심은 조위를 더해야 함)",
        ),
        sa.Column("max_dwt", sa.Float(), nullable=True, comment="접안 가능 최대 재화중량톤(DWT)"),
        sa.Column(
            "max_concurrent_vessels", sa.Integer(), nullable=False, server_default="1",
            comment="동시접안 가능 척수. berth_assignment.slot_no가 1..이 값을 씀",
        ),
        sa.Column("operator", sa.String(), nullable=True, comment="선석 운영사"),
        sa.Column("latitude", sa.Float(), nullable=True, comment="선석 대표 좌표 위도"),
        sa.Column("longitude", sa.Float(), nullable=True, comment="선석 대표 좌표 경도"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="레코드 생성 시각"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, comment="레코드 수정 시각"),
        comment="선석(부두) 제원 마스터. id는 Neo4j Berth.id와 동일 값을 공유",
    )
    op.execute("DELETE FROM berth_assignment")
    op.drop_constraint("berth_assignment_berth_id_fkey", "berth_assignment", type_="foreignkey")
    op.create_foreign_key(
        "berth_assignment_berth_id_fkey",
        "berth_assignment", "berth",
        ["berth_id"], ["id"],
        ondelete="RESTRICT",
    )
