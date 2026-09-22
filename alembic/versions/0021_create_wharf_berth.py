"""wharf/berth — 선석 제원 2계층 마스터 신설

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-19

설계 근거: docs/11_선석제원_재설계_설계문서.md

왜 신설인가
  선석 제원은 배정 감사(이 배가 이 부두에 들어갈 수 있는가)의 기준값인데,
  기존 정본 upa_berth_facility 는 접안능력이 69행 중 2행만 채워져 있다
  (공공 API 자체의 결측). UPA 웹 부두현황에서 선석 단위 제원을 정적으로
  확보해 이 공백을 메운다.

왜 두 테이블인가
  좌표·소재지·준공정보는 부두의 속성이고, 안벽길이·수심·접안능력은 선석의
  속성이다. 한 표에 합치면 부두 속성을 선석마다 복제하거나(불일치 발생)
  선석 제원을 부두로 집계해야 한다. 실측하면 같은 부두 안에서 접안능력이
  8배까지 차이 난다(2부두: 40,000 / 20,000 / 5,000 DWT).

★ 기존 자산은 건드리지 않는다 (2026-09-19 결정)
  upa_berth_facility, berth_assignment, slot_no, EXCLUDE 제약,
  mart.facility_alias 모두 그대로 둔다. 이 마이그레이션은 순수 가산이며
  기존 조회 경로에 영향이 없다.

★ 조인 입도에 대한 주의
  (facility_cd, facility_sub_code)는 **부두**를 가리킨다. 선석이 아니다.
  실측: MDU/1..8 = SK1..SK8부두, MDS/1..3 = S-Oil 1..3부두.
  portmis_vessel.arrival_facility_cd + arrival_facility_sub_code 도 같은
  체계이므로, PORTMIS 배정은 부두까지만 식별된다. 따라서 감사 기준값은
  berth 에서 **최솟값으로 집계해** wharf 수준에서 판정한다(설계문서 §6).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wharf",
        sa.Column("wharf_name", sa.String(), primary_key=True,
                  comment="부두명. 사람이 읽는 안정 키 — 로그·챗봇 응답·Neo4j에 그대로 노출된다. "
                          "★ 표기의 정본은 공공 API(upa_berth_facility.whrfNm)다 — 웹은 선석 상세를 "
                          "보태는 쪽이라, 둘이 다르면 API 표기로 맞춘다"),
        sa.Column("name_source", sa.String(10),
                  comment="UPA_API=API 표기로 확정 · UPA_WEB=API 에 없어 웹 표기를 그대로 씀"),
        # PORTMIS(arrival_facility_cd + arrival_facility_sub_code)와 붙는 조인 키.
        # 외부 코드라 PK로 쓰지 않는다 — 그쪽 사정이 우리 이력을 흔들면 안 된다.
        # 아직 확보 못 한 부두가 있어 nullable 이다(채워나가는 값이지 존재 조건이 아니다).
        sa.Column("facility_cd", sa.String(10),
                  comment="계선시설 코드(부두군). MDU=SK계열, MDS=S-Oil, MBN=신항 등"),
        sa.Column("facility_sub_code", sa.String(10),
                  comment="계선시설 서브코드 — 부두군 내의 **부두**(선석이 아님)"),
        sa.Column("port_name", sa.String(50), comment="울산본항/온산항/울산신항"),
        sa.Column("address", sa.String(300), comment="소재지"),
        sa.Column("built_year", sa.Integer()),
        sa.Column("built_by", sa.String(20), comment="준공주체(국가/민간)"),
        # 좌표는 웹에 없고 공공 API에만 있다 — 데이터가 존재하는 층위에 둔다.
        # 접안판정(선석 400m 이내)이 이 값에 의존한다.
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("berth_count", sa.Integer(), comment="자식 선석 수"),
        sa.Column("berthing_vessel_count", sa.Integer(),
                  comment="웹 접안능력의 '/N척' — 부두 전체 척수(선석별 값이 아님)"),
        # --- 감사 기준값: berth 에서 집계한 값 -----------------------------
        # MIN 이 기준이다. PORTMIS 가 부두까지만 알려주므로 어느 선석인지 모르는
        # 채 판정해야 하고, MAX 를 쓰면 실제로는 못 들어가는 배를 통과시킨다
        # (거짓 안전). MAX 는 표시·설명용으로만 함께 보관한다.
        sa.Column("min_water_depth_m", sa.Float(), comment="★ 감사 기준(안전측)"),
        sa.Column("max_water_depth_m", sa.Float(), comment="표시용"),
        sa.Column("min_capacity_dwt", sa.Float(), comment="★ 감사 기준(안전측)"),
        sa.Column("max_capacity_dwt", sa.Float(), comment="표시용"),
        sa.Column("min_length_m", sa.Float(),
                  comment="★ 감사 기준(안전측). **선석별로 확정된 값만** 집계한다"),
        sa.Column("max_length_m", sa.Float(), comment="표시용"),
        # 웹은 부두 총연장을 선석마다 복사해 싣는 경우가 있다(일반부두: 7개 선석이
        # 모두 '679m'). 그걸 선석 길이로 쓰면 어떤 배든 길이 게이트를 통과한다.
        # 총연장은 여기 따로 두고, 선석 길이가 미상일 때 **상한 검사**로만 쓴다
        # (LOA > 총연장이면 확실히 불가).
        sa.Column("total_quay_length_m", sa.Float(), comment="부두 총연장"),
        # --- 진단 ---------------------------------------------------------
        sa.Column("operator_count", sa.Integer(),
                  comment="이 부두의 선석들을 운영하는 회사 수. 1보다 크면 운영사가 선석마다 다르다"),
        sa.Column("spec_spread_flag", sa.Boolean(),
                  comment="선석별 접안능력이 서로 다른가. True면 부두 집계가 실제를 뭉갠다"),
        sa.Column("spec_source", sa.String(30), server_default="UPA_WEB"),
        comment="부두 — PORTMIS 배정과 붙는 조인 단위. 감사 기준값은 berth에서 MIN 집계한다",
    )
    # PORTMIS 조인 경로. 코드가 아직 없는 부두가 있어 UNIQUE 가 아니라 일반 인덱스다.
    op.create_index("idx_wharf_facility_code", "wharf", ["facility_cd", "facility_sub_code"])

    op.create_table(
        "berth",
        sa.Column("berth_id", sa.String(), primary_key=True,
                  comment="예: 'SK1부두-1선석'. 사람이 읽는 안정 키"),
        sa.Column("wharf_name", sa.String(), sa.ForeignKey("wharf.wharf_name", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("berth_no", sa.Integer(), comment="부두 내 선석 번호. 단일 선석 부두는 NULL"),
        sa.Column("berth_name", sa.String(200), comment="웹 표기 원문(소유구분 접두어 제거)"),
        sa.Column("ownership_prefix", sa.String(5), comment="(국)/(민)/(공)"),
        # --- 제원 ---------------------------------------------------------
        sa.Column("length_m", sa.Float(), comment="안벽길이. vessel_spec.loa_m 과 직접 비교 가능"),
        sa.Column("quay_structure", sa.String(50), comment="중력식/잔교식/강관돌핀 등"),
        sa.Column("length_basis", sa.String(24),
                  comment="length_m 의 근거. BERTH=선석별 확정 · WHARF_TOTAL=총연장 복사라 미상 · "
                          "AMBIGUOUS/WHARF_TOTAL_SUSPECT=대조 실패라 미상"),
        # ★ water_depth_m 로 명시한다. vessel_spec.depth_m 은 **선박 깊이**(molded
        #   depth)이고 이쪽은 **수심**이다. 같은 쿼리에서 만나면 사고가 난다.
        sa.Column("water_depth_m", sa.Float(), comment="수심. 원문이 범위면 최솟값(안전측)"),
        sa.Column("water_depth_is_range", sa.Boolean(),
                  comment="원문이 '9~11m' 같은 범위였는가"),
        sa.Column("capacity_value", sa.Float(),
                  comment="접안능력(DWT). 원문이 부두 구성 목록형이면 선석 단위로 확정 불가라 NULL"),
        sa.Column("capacity_unit", sa.String(10)),
        sa.Column("capacity_suspect", sa.Boolean(), server_default=sa.text("false"),
                  comment="단위가 DWT가 아님 — 접안능력 칸에 수심값이 들어간 행이 실제로 있다"),
        sa.Column("unload_value", sa.Float()),
        sa.Column("unload_unit", sa.String(20),
                  comment="'천 톤'(연간 처리량)과 'Bbls'(유량)가 섞인다. 차원이 달라 수치 비교 금지"),
        sa.Column("handling_cargo_name", sa.String(200)),
        # ★ 운영사는 부두가 아니라 선석의 속성이다. 실측: 6부두의 선석들을
        #   고려항만·울산항6,7부두운영·한국보팍터미날 세 회사가 나눠 운영한다.
        sa.Column("operator_name", sa.String(100)),
        # --- 원문 보존 -----------------------------------------------------
        # 파싱 규칙은 반드시 틀린 케이스가 나온다. 재수집 없이 고칠 수 있어야 한다.
        sa.Column("quay_length_raw", sa.String(200)),
        sa.Column("water_depth_raw", sa.String(200)),
        sa.Column("capacity_raw", sa.String(300)),
        sa.Column("unload_raw", sa.String(200)),
        sa.Column("port_code", sa.String(20),
                  comment="UPA 웹 상세 페이지 코드. 출처 추적용 — 페이지네이션 부산물이라 키로 쓰지 않는다"),
        sa.Column("spec_source", sa.String(30), server_default="UPA_WEB"),
        sa.Column("name_source", sa.String(10),
                  comment="이 선석이 속한 부두 이름의 출처. UPA_API 가 정본"),
        comment="선석 — 제원의 실체. 부두 집계값의 근거이자 병목 선석 추적용",
    )
    op.create_index("idx_berth_wharf_name", "berth", ["wharf_name"])


def downgrade() -> None:
    op.drop_index("idx_berth_wharf_name", table_name="berth")
    op.drop_table("berth")
    op.drop_index("idx_wharf_facility_code", table_name="wharf")
    op.drop_table("wharf")
