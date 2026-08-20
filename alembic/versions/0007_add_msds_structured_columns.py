"""add structured value columns to msds_chemical

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-02

MSDS의 "값 하나짜리" 항목을 msds_payload(JSONB) 안에서 꺼내 일반 컬럼으로 승격한다.

왜 필요한가 — 이 값들은 원래 정형 데이터인데 지금은 벡터 검색으로 답하고 있다.
2026-08-02 실측: "벤젠 인화점 몇 도야?" 질문에서 상위 6청크가

    detail05 0.3443 · detail06 0.3430 · detail02 0.3390 · detail09 0.3390 · ...

로, 정답(detail09)과 오답의 점수 차가 0.005 이내라 실행할 때마다 순위가 뒤집힌다.
`인화점: -11 ℃`라는 확정된 값이 원문에 있는데도 근사 검색에 맡기고 있었던 셈이다.

왜 지금까지 없었나 — 추출 로직은 이미 있다. data-pipeline의
msds_preprocessor._flatten_record()가 아래 코드로 값을 뽑지만, msds_pg_loader의
INSERT가 식별자 컬럼 + msds_payload만 적재해서 백엔드까지 오지 않았다. 이 마이그레이션은
그 단절을 메우고, 로더도 같은 컬럼을 채우도록 함께 수정한다.

이 마이그레이션은 스키마 변경 + 기존 35행 백필까지 수행한다 — 원본이 이미
msds_payload에 통째로 들어있으므로 data-pipeline을 다시 돌릴 필요가 없다.

컬럼에 넣지 않은 것:
  - IMDG 등급(detail14 N06) — (:Chemical)-[:HAS_IMDG_CLASS]->(:ImdgClass) 관계로
    Neo4j에 이미 있고, 등급 간 SEGREGATE 순회로 격리 판정에 쓰인다. 관계가 필요한
    값은 그래프, 값만 필요한 것은 컬럼 — 이 구분을 흐리지 않는다.
  - UN번호 — msds_chemical.un_no로 이미 승격돼 있다.
  - GHS 분류(B02) — (:Chemical)-[:HAS_HAZARD]->(:HazardClass)로 이미 그래프에 있다.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (컬럼, 섹션, msdsItemCode, 타입) — msds_preprocessor._flatten_record()의 코드 매핑과 동일.
_COLUMNS: list[tuple[str, str, str, sa.types.TypeEngine]] = [
    ("flash_point_text", "detail09", "I14", sa.String(300)),
    ("boiling_point_text", "detail09", "I12", sa.String(300)),
    ("vapor_pressure_text", "detail09", "I22", sa.String(300)),
    ("specific_gravity_text", "detail09", "I28", sa.String(300)),
    ("packing_group", "detail14", "N08", sa.String(20)),
    ("ems_fire", "detail14", "N1202", sa.String(20)),
    ("ems_spill", "detail14", "N1204", sa.String(20)),
    ("signal_word", "detail02", "B0404", sa.String(50)),
    ("exposure_limit_kr", "detail08", "H0202", sa.Text()),
]

# KOSHA API가 값 없음을 나타내는 문자열들. NULL로 정규화하지 않으면
# "인화점: 자료없음"이 값처럼 프롬프트에 실려 LLM이 근거로 인용한다.
_NULL_VALUES = ("자료없음", "해당없음", "-", "", "N/A", "없음")

# 원문에 HTML 엔티티가 그대로 들어있다(예: 인화점 "&lt; 20 ℃", 독성 "LD50 &gt;2000").
# 컬럼으로 승격하면서 사람이 읽는 형태로 되돌린다.
#
# 두 번 적용하는 이유 — 이중 이스케이프된 값이 실제로 있다. detail08 호흡기 보호의
# "산소가 부족한 경우(&amp;lt;19.6%)"는 한 번 풀면 "&lt;19.6%"에서 멈춘다.
# (backend chunking.unescape() / data-pipeline msds_pg_loader._unescape()는 변화가
#  없을 때까지 반복한다. SQL에서는 루프가 번거로워 실측 최대 깊이 2회로 고정했다.)
_UNESCAPE = [("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&quot;", '"')]
_UNESCAPE_PASSES = 2


def _item_expr(section: str, code: str) -> str:
    """msds_payload에서 해당 itemCode의 itemDetail을 꺼내 HTML 엔티티까지 되돌린 SQL 식."""
    expr = (
        f"btrim((SELECT i->>'itemDetail' "
        f"FROM jsonb_array_elements(msds_payload->'{section}'->'data') i "
        f"WHERE i->>'msdsItemCode' = '{code}' LIMIT 1))"
    )
    for _ in range(_UNESCAPE_PASSES):
        for entity, char in _UNESCAPE:
            expr = f"replace({expr}, '{entity}', '{char}')"
    return expr


def upgrade() -> None:
    for name, _section, _code, type_ in _COLUMNS:
        op.add_column("msds_chemical", sa.Column(name, type_, nullable=True))
    op.add_column("msds_chemical", sa.Column("flash_point_celsius", sa.Float(), nullable=True))

    # 백필 — jsonb_array_elements는 detail 섹션이 없거나 data가 배열이 아니면
    # 행을 만들지 않으므로 서브쿼리가 NULL을 반환한다(예외 아님).
    null_list = ", ".join(f"'{v}'" for v in _NULL_VALUES)
    for name, section, code, _type in _COLUMNS:
        expr = _item_expr(section, code)
        op.execute(
            f"UPDATE msds_chemical SET {name} = "
            f"CASE WHEN coalesce({expr}, '') IN ({null_list}) THEN NULL "
            f"ELSE {expr} END "
            f"WHERE jsonb_typeof(msds_payload->'{section}'->'data') = 'array'"
        )

    # flash_point_celsius — 텍스트에서 첫 숫자를 뽑는다. "&lt; 20 ℃"(→ "< 20 ℃"),
    # "-11 ℃", "-1~565 ℃"처럼 부등호·범위·단위·출처가 섞여 있어 항상 성공하지는
    # 않는다. 실패하면 NULL로 두고 원문(flash_point_text)을 그대로 쓴다 —
    # 파싱값을 억지로 만들어 잘못된 수치를 답변에 싣는 것보다 낫다.
    op.execute(
        "UPDATE msds_chemical SET flash_point_celsius = "
        "(substring(flash_point_text FROM '-?[0-9]+\\.?[0-9]*'))::float "
        "WHERE flash_point_text ~ '-?[0-9]+\\.?[0-9]*'"
    )


def downgrade() -> None:
    op.drop_column("msds_chemical", "flash_point_celsius")
    for name, _section, _code, _type in reversed(_COLUMNS):
        op.drop_column("msds_chemical", name)
