"""create berth and berth_assignment tables

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-13

선석(Berth) 점유 관리 스키마 — 관제사 페르소나별 분석(온산MVP 우선순위 1번)이
지목한 최우선 공백을 메운다: "판정"만 있고 "확정을 시간구간으로 예약해 저장하는"
테이블이 없어 동시 배정 레이스컨디션이 이론적으로 가능했던 문제.

`available_count`/`is_occupied` 같은 잔여수량·불리언 컬럼은 의도적으로 두지 않는다
— 선석은 시간 축을 가진 자원이므로 시간구간 예약(tstzrange + EXCLUDE) 모델로만
다룬다.

`berth.id`는 Neo4j `Berth.id`(wharf_name 기반 문자열, 예: "SK2부두(민유)")를 그대로
재사용한다 — 기존 dashboard/scheduling 코드 전반이 이미 이 문자열로 선석을
식별하므로 새 ID 매핑 계층을 만들지 않는다. 화물군(HANDLES)·인접관계(ADJACENT_TO)는
Neo4j가 정본이라 여기 중복 저장하지 않는다.

동시접안 N척 가능한 선석(소형 선석 등)은 슬롯 분해로 처리한다 — berth 행을
쪼개지 않고 berth_assignment.slot_no(1..berth.max_concurrent_vessels)를 EXCLUDE
제약의 등가 키에 포함시켜, 같은 선석의 서로 다른 슬롯은 독립적으로 겹칠 수 있게
한다. 이렇게 하면 동시성 안전장치가 애플리케이션의 락 규율이 아니라 DB 제약
자체에 있어, 어떤 경로로 INSERT하든(오케스트레이터/관리자 도구/향후 배치 스크립트)
우회가 불가능하다.

CANCELLED/COMPLETED 상태는 EXCLUDE 제약의 WHERE 절에서 제외한다 — 취소된 예약이
같은 시간대 재예약을 영구히 막으면 안 된다.

(재작성 메모, 2026-08-16 야간) 이 파일은 원래 존재했다가 소스가 삭제됐다.
`alembic/versions/__pycache__`에 남아있던 컴파일된 .pyc의 문자열 상수로부터
설계 취지(위 문단들)를 복구했고, 정확한 컬럼·타입·제약은 이미 이 스키마가
적용된 상태로 남아있던 실제 개발 DB(information_schema/pg_constraint)를
직접 조회해 그대로 옮겨적었다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, ExcludeConstraint

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIVE_STATUSES = ("REQUESTED", "APPROVED", "SCHEDULED", "BERTHED")
_ALL_STATUSES = _ACTIVE_STATUSES + ("COMPLETED", "CANCELLED", "REJECTED")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

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

    op.create_table(
        "berth_assignment",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="PK"),
        sa.Column(
            "berth_id", sa.String(), sa.ForeignKey("berth.id", ondelete="RESTRICT"), nullable=True,
            comment="배정된 선석 ID(berth.id FK). 예약이 성립하지 않은 반려(REJECTED) 건은 NULL일 수 있음",
        ),
        sa.Column(
            "slot_no", sa.Integer(), nullable=False, server_default="1",
            comment="1..berth.max_concurrent_vessels. 동시접안 슬롯 번호",
        ),
        sa.Column("call_sign", sa.String(), nullable=True, comment="선박 호출부호. 입항 전 사전 승인 단계엔 이것만 있을 수 있음"),
        sa.Column("imo_no", sa.String(), nullable=True, comment="선박 IMO 번호"),
        sa.Column("vessel_name", sa.String(), nullable=True, comment="선박명"),
        sa.Column(
            "vessel_uid", sa.String(), nullable=True,
            comment="파이프라인 표준 선박 식별자(MMSI-First). 사전신고 시점엔 call_sign/"
            "vessel_name만 있을 수 있어 nullable — AIS로 확인되면 후속 채움",
        ),
        sa.Column("cargo_chem_id", sa.String(), nullable=True, comment="화물 화학물질 식별자(msds_chemical.chem_id)"),
        sa.Column("cargo_cas_no", sa.String(), nullable=True, comment="화물 CAS 등록번호"),
        sa.Column(
            "planned_window", TSTZRANGE(), nullable=True,
            comment="계획된 접안 시간구간(TSTZRANGE, [접안예정~출항예정)). 같은 berth_id·slot_no에서 활성 "
            "상태끼리 겹치면 EXCLUDE 제약이 INSERT를 막음. REJECTED 건은 NULL일 수 있음",
        ),
        sa.Column(
            "actual_berthing_at", sa.DateTime(timezone=True), nullable=True,
            comment="실제 접안이 확인된 시각(계획이 아니라 사후 확인값 — upa_port_call 등으로 대조)",
        ),
        sa.Column("actual_departure_at", sa.DateTime(timezone=True), nullable=True, comment="실제 출항이 확인된 시각"),
        sa.Column(
            "status", sa.String(), nullable=False, server_default="REQUESTED",
            comment="예약 상태: REQUESTED(요청)/APPROVED(승인)/SCHEDULED(배정확정)/BERTHED(접안중)/"
            "COMPLETED(완료)/CANCELLED(취소)/REJECTED(반려). REQUESTED~BERTHED만 겹침 방지 대상(ACTIVE_STATUSES)",
        ),
        sa.Column(
            "assignment_score", sa.Float(), nullable=True,
            comment="배정 알고리즘 점수. 이번 라운드는 컬럼만 — 채우는 로직은 후속(C단계)",
        ),
        sa.Column(
            "assignment_reason", sa.Text(), nullable=True,
            comment="배정/승인/반려 판단 사유 설명(관제사 조회 및 감사용, 오케스트레이터 summary 등을 담음)",
        ),
        sa.Column(
            "rejected_candidates", JSONB(), nullable=True,
            comment="검토했으나 탈락한 후보와 탈락 사유 (설명가능성 요구사항)",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="레코드 생성 시각(=결정이 내려진 시각)"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, comment="레코드 수정 시각"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _ALL_STATUSES) + ")",
            name="ck_berth_assignment_status",
        ),
        comment="선석 점유/예약 기록. 관제사 승인·스케줄링 에이전트 배정을 시간구간(planned_window)으로 "
        "저장하는 이 프로젝트의 핵심 테이블 — \"판정은 있지만 기록이 없다\"는 공백을 메우는 자리. "
        "같은 선석·슬롯의 활성 예약끼리 겹치면 DB 레벨 EXCLUDE 제약이 막음",
    )
    op.create_index("idx_berth_assignment_berth_id", "berth_assignment", ["berth_id"])
    op.create_index("idx_berth_assignment_vessel_uid", "berth_assignment", ["vessel_uid"])

    op.execute(
        "ALTER TABLE berth_assignment "
        "ADD CONSTRAINT ex_berth_assignment_no_overlap "
        "EXCLUDE USING gist ("
        "    berth_id WITH =,"
        "    slot_no WITH =,"
        "    planned_window WITH &&"
        ") WHERE (status IN (" + ", ".join(f"'{s}'" for s in _ACTIVE_STATUSES) + "))"
    )


def downgrade() -> None:
    op.drop_table("berth_assignment")
    op.drop_table("berth")
