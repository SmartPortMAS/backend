"""portmis_vessel에 계선시설 서브코드 + 화물톤수 계열 컬럼 추가

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-13

배경
----
PORT-MIS raw 응답(collect_portmis.py가 arrival_/departure_ 접두로 평탄화한 것)에는
아래 필드가 이미 들어오고 있었는데, portmis_preprocessor.py의 COLUMN_MAP에
누락돼 전처리 단계에서 조용히 버려지고 있었다(2026-09-13 raw JSON 직접 확인으로
발견 — arrival_laidupFcltySubCd 등이 raw dict 키에는 있으나 COLUMN_MAP에 없었음).

  arrival_laidupFcltySubCd / departure_laidupFcltySubCd
      계선시설 서브코드. upa_port_call.facility_spec_sub_code와 같은 체계라
      시설코드 조인을 wharf 단위가 아니라 선석 단위로 정밀화하는 데 필요.
      (0011의 "arrival_facility_cd는 laidupFcltyCd-SubCd 결합형(WAM-01)" 주석은
       실제 COLUMN_MAP 구현과 맞지 않았다 — 코드만 들어가고 서브코드는 누락돼
       있었다. 이번에 별도 컬럼으로 분리해 그 간극을 메운다.)
  arrival_intrlGrtg   국제총톤수. gross_tonnage(국내 총톤수, grtg)와 다른 값.
  arrival_ldadngFrghtClCd / arrival_ldadngTon / arrival_trnpdtTon /
  arrival_landngFrghtTon / departure_ldFrghtTon
      화물명세·화물톤수 실측치. upa_cargo_manifest가 전량 합성(is_synthetic=True)
      데이터인 것과 달리 이건 PORT-MIS 실제 신고값이다.

선원수(crewCo 등)·도선여부(piltgYn)는 이번에 의도적으로 제외한다 — 판정에 쓸
용도가 아직 없고(선원수는 불필요, 도선여부는 액체화물선 특성상 거의 상수로
나올 것으로 예상돼 변별력이 낮음), 필요해지면 이 마이그레이션과 같은 패턴으로
추가하면 된다.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "portmis_vessel",
        sa.Column(
            "arrival_facility_sub_code", sa.String(length=10), nullable=True,
            comment="입항 시점 계선시설 서브코드(arrival_laidupFcltySubCd). "
            "upa_port_call.facility_spec_sub_code와 같은 체계",
        ),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column(
            "departure_facility_sub_code", sa.String(length=10), nullable=True,
            comment="출항 시점 계선시설 서브코드(departure_laidupFcltySubCd)",
        ),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column(
            "intrl_gross_tonnage", sa.Numeric(), nullable=True,
            comment="국제총톤수(arrival_intrlGrtg). gross_tonnage(국내 총톤수)와는 다른 값",
        ),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column("cargo_class_code", sa.String(length=10), nullable=True, comment="화물명세 코드(arrival_ldadngFrghtClCd)"),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column(
            "cargo_onboard_ton", sa.Numeric(), nullable=True,
            comment="입항 시 적재 중인 화물 총톤수(arrival_ldadngTon)",
        ),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column("cargo_transship_ton", sa.Numeric(), nullable=True, comment="환적톤수(arrival_trnpdtTon)"),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column(
            "cargo_unload_ton", sa.Numeric(), nullable=True,
            comment="이 항에서 양하(하역)한 화물톤수 — 입항상세(arrival_landngFrghtTon)",
        ),
    )
    op.add_column(
        "portmis_vessel",
        sa.Column(
            "cargo_load_ton", sa.Numeric(), nullable=True,
            comment="이 항에서 적하(선적)한 화물톤수 — 출항상세(departure_ldFrghtTon)",
        ),
    )


def downgrade() -> None:
    op.drop_column("portmis_vessel", "cargo_load_ton")
    op.drop_column("portmis_vessel", "cargo_unload_ton")
    op.drop_column("portmis_vessel", "cargo_transship_ton")
    op.drop_column("portmis_vessel", "cargo_onboard_ton")
    op.drop_column("portmis_vessel", "cargo_class_code")
    op.drop_column("portmis_vessel", "intrl_gross_tonnage")
    op.drop_column("portmis_vessel", "departure_facility_sub_code")
    op.drop_column("portmis_vessel", "arrival_facility_sub_code")
