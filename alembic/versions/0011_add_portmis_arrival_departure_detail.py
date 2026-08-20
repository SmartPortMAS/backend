"""add portmis_vessel arrival/departure detail columns (facility, tonnage, agency, timestamps)

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-16

PORT-MIS API 응답은 <details><detail type="입항"|"출항">...</detail></details>
이중 중첩 구조로 실제 일시·접안시설·총톤수·대리점명을 내려주는데, 그동안
수집기(portmis_collector.py)의 아이템 파서가 item의 직계 자식만 읽는 구조라 두
단계 아래 중첩된 이 필드들을 전혀 읽지 못했다 — departure_sched_utc/
dest_arrival_utc가 늘 NULL이었던 것도 이 버그가 원인이었다(값이 없는 게 아니라
엉뚱한 위치를 읽고 있었다).

실측(200건+): 이 항차에 detail은 "입항"/"출항" 딱 두 종류뿐이고(그 이상 나온 적
없음), 아직 출항 안 한 진행중 항차는 "입항" 1개뿐이다 — 카디널리티가 닫혀 있어
(최대 2, 항상 이 두 타입 중에서만) upa_port_call처럼 별도 이벤트 테이블로 안
쪼개고 기존 portmis_vessel 행에 arrival_*/departure_* 컬럼 쌍으로 폭을 넓히는
쪽을 택했다(1항차=1행 그레인 유지, 기존 callsgn 조인 소비처 전부 그대로 재사용
가능).

gross_tonnage/agency_name은 입항·출항 두 detail에 값이 중복 보고돼(실측: 항상
일치) 접두어 없이 한 컬럼으로 병합한다.

`arrival_facility_cd`/`arrival_facility_nm`("공식 배정 계선시설")은 입항허가
시점에 PORT-MIS가 공식적으로 배정한 선석/정박지를 나타낸다 — 실시간 위치 기반
추정치(upa_port_call.facility_name)보다 신뢰도가 높은 소스로,
07_입항승인_선석확정_설계문서.md의 핵심 입력으로 쓰인다.

(재작성 메모, 2026-08-16 야간) 이 파일은 원래 존재했다가 소스가 삭제됐다.
`alembic/versions/__pycache__`에 남아있던 컴파일된 .pyc의 문자열 상수(위 근본
원인 설명 포함)로부터 설계 취지를 복구했고, 정확한 컬럼·타입은 이미 이 스키마가
적용된 상태로 남아있던 실제 개발 DB를 직접 조회해 그대로 옮겨적었다.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "portmis_vessel",
        sa.Column("arrival_at_utc", sa.DateTime(timezone=True), nullable=True, comment="공식 입항일시(arrival_etryptDt)"),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column(
            "arrival_facility_cd", sa.String(length=20), nullable=True,
            comment="공식 배정 계선시설 코드(laidupFcltyCd-SubCd, 예: WAM-01). ulsan_facility_codes 키와 동일 형식",
        ),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column("arrival_facility_nm", sa.String(length=100), nullable=True, comment="위 코드의 공식 시설명(부두/돌핀/정박지)"),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column("departure_at_utc", sa.DateTime(timezone=True), nullable=True, comment="실제 출항일시(departure_tkoffDt)"),
    )
    op.add_column("portmis_vessel", sa.Column("departure_facility_cd", sa.String(length=20), nullable=True))
    op.add_column("portmis_vessel", sa.Column("departure_facility_nm", sa.String(length=100), nullable=True))
    op.add_column(
        "portmis_vessel",
        sa.Column("gross_tonnage", sa.Integer(), nullable=True, comment="총톤수(GT). 입항/출항 상세 중 값 있는 쪽"),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column("agency_name", sa.String(length=200), nullable=True, comment="선박관리회사/대리점명. 입항/출항 상세 중 값 있는 쪽"),
    )

    # 기존 컬럼 2개의 주석을 갱신한다 — "실측상 대부분 NULL, 신뢰해 쓰지 말 것"은
    # 수집기 버그로 인한 상태를 설명한 것이었는데, 이 마이그레이션이 그 버그를
    # 고쳤으므로 더 이상 사실이 아니다. 낡은 경고를 그대로 두면 고쳐진 뒤에도
    # 관제사·개발자가 계속 이 필드를 불신하게 된다.
    op.alter_column(
        "portmis_vessel", "departure_sched_utc",
        existing_type=sa.DateTime(timezone=True),
        comment="출항예정일시(입항 선박에 한함, API 2025.03 추가 필드). arrival_tkoffPrrrnDt에서 옴 — "
        "이 마이그레이션 전에는 수집기 버그로 실측상 대부분 NULL이었으나 지금은 채워짐",
    )
    op.alter_column(
        "portmis_vessel", "dest_arrival_utc",
        existing_type=sa.DateTime(timezone=True),
        comment="목적지 입항예정일시(출항 선박에 한함). departure_dstnEtryptDt에서 옴 — "
        "이 마이그레이션 전에는 수집기 버그로 실측상 대부분 NULL이었으나 지금은 채워짐",
    )
    op.execute(
        "COMMENT ON TABLE portmis_vessel IS "
        "'해수부 PORT-MIS(VsslEtrynd5) 입출항 신고 원본. 관제사가 승인한 기록이 아니라 "
        "선사/대리점이 제출하는 행정 신고 데이터. 이 프로젝트에서 액체화물선(유조선) 판별의 정본. "
        "arrival_at_utc/departure_at_utc가 실제 입출항 일시를 담는다(이 마이그레이션 이전엔 "
        "이 필드들이 없어 정확한 일시를 알 방법이 없었음)'"
    )


def downgrade() -> None:
    op.execute(
        "COMMENT ON TABLE portmis_vessel IS "
        "'해수부 PORT-MIS(VsslEtrynd5) 입출항 신고 원본. 관제사가 승인한 기록이 아니라 "
        "선사/대리점이 제출하는 행정 신고 데이터 — 정확한 입출항 일시 필드가 없다는 한계가 있음. "
        "이 프로젝트에서 액체화물선(유조선) 판별의 정본'"
    )
    op.alter_column(
        "portmis_vessel", "dest_arrival_utc",
        existing_type=sa.DateTime(timezone=True),
        comment="목적지 입항예정일시(출항 선박에 한함). 마찬가지로 실측상 대부분 NULL",
    )
    op.alter_column(
        "portmis_vessel", "departure_sched_utc",
        existing_type=sa.DateTime(timezone=True),
        comment="출항예정일시(입항 선박에 한함, API 2025.03 추가 필드). 실측상 대부분 NULL — 신뢰해 쓰지 말 것",
    )
    op.drop_column("portmis_vessel", "agency_name")
    op.drop_column("portmis_vessel", "gross_tonnage")
    op.drop_column("portmis_vessel", "departure_facility_nm")
    op.drop_column("portmis_vessel", "departure_facility_cd")
    op.drop_column("portmis_vessel", "departure_at_utc")
    op.drop_column("portmis_vessel", "arrival_facility_nm")
    op.drop_column("portmis_vessel", "arrival_facility_cd")
    op.drop_column("portmis_vessel", "arrival_at_utc")
