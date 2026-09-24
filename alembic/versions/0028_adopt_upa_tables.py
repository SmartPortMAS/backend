"""adopt upa_* tables — data-pipeline auto_create 표 5종을 Alembic 소유로

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-24

왜
--
스키마 주인이 셋이었다 — Alembic, data-pipeline 로더(auto_create=True),
data-pipeline/mart_views.sql 의 CREATE TABLE 껍데기. 그래서 빈 DB(첫 운영 배포,
2026-09-24 RDS)에서 upgrade 가 0020 에서 깨졌고, 뷰 파일은 표가 없을 때를 대비해
컬럼 일부만 가진 껍데기를 따로 들고 있었다(mart_views.sql 0-A 절 주석).

2026-09-24 결정: 표·뷰 구조는 전부 backend(Alembic)가 만든다. data-pipeline 은
적재만 한다(auto_create=False — 표가 없으면 "alembic upgrade head 먼저" 오류).

무엇을
------
upa_vessel_position · upa_port_call · upa_cargo_manifest · upa_berth_facility ·
upa_anchorage. 정의는 로컬 개발 DB 에서 pg_dump -s 로 뜬 그대로다(../sql/0028_upa_tables.sql).
컬럼 타입이 pandas 추론값(sog bigint, ptent_yr double precision 등)인 것도 그대로 둔다 —
로더가 넣는 값과 어긋나지 않는 게 우선이다.

유니크 인덱스는 로더의 ON CONFLICT 키(upa_loader.TABLE_MAP)만 만든다. 로컬 DB 에 남아
있는 record_uid 유니크 인덱스(upa_*_uidx, 예전 키 체계의 흔적)는 새 DB 에 만들지 않는다.
적재 키 인덱스가 이미 있으면 로더의 _ensure_unique_index 가 중복 정리 DELETE 를
하지 않는다(common_pg_loader) — 이 인덱스를 여기서 거는 이유 중 하나다.

이미 표가 있는 DB(로컬 개발 DB 등)에서는 IF NOT EXISTS 라 아무것도 바뀌지 않는다.
COMMENT 는 덮어쓰지만 로컬 DB 에서 뜬 문구 그대로라 결과가 같다. (그 COMMENT 를 처음
누가 어디서 달았는지는 두 레포에서 찾지 못했다.)

ulsan_vessel_mart 는 옮기지 않는다 — 0027 이 폐기한 표다.

되돌리기
--------
downgrade 는 아무것도 지우지 않는다. 적재된 운영 데이터가 든 표라, 소유권을 되돌린다고
표를 지우면 안 된다.
"""
from pathlib import Path
from typing import Sequence, Union

from sqlalchemy.util import await_only

from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL = Path(__file__).resolve().parent.parent / "sql" / "0028_upa_tables.sql"


def _exec_sql_file(path: Path) -> None:
    # 여러 문장이 든 SQL 파일을 그대로 실행한다. op.execute 는 문장 하나씩 prepare 하고
    # (asyncpg 는 prepared statement 에 여러 문장을 못 넣는다) 문자열 속 ':이름' 을 바인드
    # 변수로 읽는다. asyncpg 의 execute(인자 없음)는 simple query 프로토콜이라 둘 다 없다.
    # 같은 연결이라 alembic 트랜잭션 안에서 돈다.
    raw = op.get_bind().connection.driver_connection
    await_only(raw.execute(path.read_text(encoding="utf-8")))


def upgrade() -> None:
    _exec_sql_file(_SQL)


def downgrade() -> None:
    pass
