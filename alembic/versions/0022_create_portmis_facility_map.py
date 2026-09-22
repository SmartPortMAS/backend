"""portmis_facility_map — PORT-MIS 계선시설 ↔ wharf/berth 매핑

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-19

설계 근거: docs/11_선석제원_재설계_설계문서.md §3·§15

왜 별도 표인가
  배정의 정본은 portmis_vessel.arrival_facility_cd + arrival_facility_sub_code 다.
  이 코드를 upa_berth_facility 의 fcltCd/fcltSubCd 와 같은 체계로 보고 바로
  조인하려 했으나, 실측하면 **다른 레지스트리**다:

      PORT-MIS : MBU/01 = 'SK2부두 01',  MBU/11 = 'SK1부두 11'
      UPA      : MDU/02 = 'SK2부두',     MDU/01 = 'SK1부두'

  3글자 접두 공간은 공유하지만 배정이 달라, 그대로 조인하면 대부분 빗나가고
  일부는 우연히 맞아 오히려 더 나쁘다. 그래서 PORT-MIS 가 쓰는 코드를 PORT-MIS
  원본에서 직접 수집해 우리 wharf/berth 에 연결한 표를 둔다.

입도가 시설마다 다르다
  PORT-MIS 는 '자동차부두 01/02/03' 처럼 선석 단위로 등록된 시설과
  'SK3부두'·'S-OIL1부두' 처럼 부두 통째로 등록된 시설이 섞여 있다.
  그래서 berth_id 는 nullable 이다 —

      berth_id 있음 : 선석이 특정된다. 그 선석 제원으로 정확히 판정
      berth_id 없음 : 부두까지만 안다. 그 부두의 최악값(MIN)으로 보수 판정

  "어느 선석인지 모른다"를 NULL 로 표현하고, 판정 쪽에서 보수적으로 처리한다.
  모르는 것을 아는 척하지 않는다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "portmis_facility_map",
        sa.Column("facility_cd", sa.String(10), primary_key=True,
                  comment="PORT-MIS laidupFcltyCd"),
        sa.Column("facility_sub_code", sa.String(10), primary_key=True,
                  comment="PORT-MIS laidupFcltySubCd. 이름 끝 숫자와 같은 값이며 "
                          "반드시 선석 번호는 아니다(SK1부두는 11·12로 등록돼 있다)"),
        sa.Column("facility_nm", sa.String(100),
                  comment="PORT-MIS 시설명 원문. 예: '자동차부두 02', 'S-OIL3부두'"),
        # nullable 이다 — 붙이지 못한 시설도 **행으로 남긴다**. 목록에서 지우면
        # 무엇이 빠졌는지 알 수 없고, 감사에서 그냥 사라진다.
        sa.Column("wharf_name", sa.String(), sa.ForeignKey("wharf.wharf_name", ondelete="CASCADE"),
                  nullable=True),
        # 선석이 특정될 때만 채운다. NULL 이면 부두 단위 보수 판정으로 떨어진다.
        sa.Column("berth_id", sa.String(), sa.ForeignKey("berth.berth_id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("match_level", sa.String(12), nullable=False,
                  comment="BERTH=선석까지 특정 · WHARF=부두까지만 · "
                          "KNOWN_GAP=제원 자료 없음이 확인됨 · UNMAPPED=아직 미처리"),
        sa.Column("confidence", sa.String(24),
                  comment="AUTO_COUNT_MATCH(시설수=선석수라 순서대로 대응) | "
                          "COUNT_MISMATCH(부두 단위로만) | MANUAL(사람이 지정) | "
                          "CONFIRMED_NO_SOURCE(공백 확인) | TODO(작업 대기)"),
        # KNOWN_GAP 과 UNMAPPED 를 가르는 것이 이 컬럼이다. 사유가 적혀 있으면
        # "찾아봤고 없다"는 뜻이고, 비어 있으면 "아직 안 봤다"는 뜻이다.
        # 둘을 섞으면 작업 대기열이 줄지 않는다.
        sa.Column("gap_reason", sa.Text(),
                  comment="못 붙인 사유. KNOWN_GAP 일 때만 채워진다"),
        comment="PORT-MIS 계선시설 ↔ wharf/berth. 배정 감사의 조인 경로. "
                "배정 건수 같은 파생 통계는 두지 않는다 — portmis_vessel 에서 "
                "직접 세면 항상 정확하고, 여기 저장하면 수집이 진행될수록 낡는다",
    )
    op.create_index("idx_portmis_facility_map_berth", "portmis_facility_map", ["berth_id"])
    op.create_index("idx_portmis_facility_map_wharf", "portmis_facility_map", ["wharf_name"])


def downgrade() -> None:
    op.drop_index("idx_portmis_facility_map_wharf", table_name="portmis_facility_map")
    op.drop_index("idx_portmis_facility_map_berth", table_name="portmis_facility_map")
    op.drop_table("portmis_facility_map")
