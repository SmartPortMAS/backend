"""cargo manifest upsert by bl_no — 합성 화물 적재를 TRUNCATE 교체에서 bl_no UPSERT 로

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-26

왜
--
data-pipeline gen_cargo_manifest 는 적재 때마다 표를 TRUNCATE 하고 전부 다시 넣었다.
화물이 매번 무작위로 바뀌던 v3 까지는 자연키가 없어 그럴 수밖에 없었다(record_uid =
행 전체 해시). v4 부터 bl_no 가 입항 건 키로 결정적이다(SYN-{콜사인}_{연도}_{횟수}-{순번},
위반 주입은 BL900001~). 그래서 bl_no 를 적재 키로 쓰고 TRUNCATE 를 없앤다.
  · TRUNCATE 와 재적재 사이에 표가 비는 순간이 사라진다.
  · 스케줄러가 주기적으로 돌려도 안전하다(같은 입항 건은 같은 행).

무엇을
------
  1. 표를 비운다 — 2026-09-26 결정: 기존 행(v3 이전 콜사인 키 행 포함)은 버리고
     생성기로 다시 만든다. 생성기가 입항 건 키로 결정적이라 최근 30일 입항 건은 대부분
     같은 화물로 돌아온다. 이 마이그레이션 뒤에 data-pipeline 에서
     `py gen_cargo_manifest.py --load` 를 한 번 돌려야 화물이 다시 생긴다.
  2. record_uid 유니크 인덱스를 지운다(예전 적재 키).
  3. bl_no 유니크 인덱스를 만든다 — upa_loader.TABLE_MAP 의 ON CONFLICT 키.
     이름은 common_pg_loader._ensure_unique_index 규칙(표_uidx__컬럼)과 같게 둔다.

되돌리기
--------
인덱스만 되돌린다. 비운 행은 복구하지 않는다(합성 데이터라 생성기로 다시 만든다).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("TRUNCATE TABLE public.upa_cargo_manifest")
    op.execute("DROP INDEX IF EXISTS public.upa_cargo_manifest_uidx__record_uid")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS upa_cargo_manifest_uidx__bl_no "
        "ON public.upa_cargo_manifest USING btree (bl_no)"
    )
    op.execute(
        "COMMENT ON COLUMN public.upa_cargo_manifest.record_uid IS "
        "'미사용(NULL). 예전 적재 키 — 0031 부터 bl_no 가 UPSERT 키'"
    )
    op.execute(
        "COMMENT ON COLUMN public.upa_cargo_manifest.bl_no IS "
        "'선화증권(B/L)번호(합성값, UPSERT 키). 정상 행은 SYN-{콜사인}_{입항연도}_{입항횟수}-{순번}, "
        "위반 시나리오 행은 BL9 로 시작'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.upa_cargo_manifest_uidx__bl_no")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS upa_cargo_manifest_uidx__record_uid "
        "ON public.upa_cargo_manifest USING btree (record_uid)"
    )
