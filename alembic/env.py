"""Alembic 환경 설정.

[include_object — 남의 테이블을 DROP 하지 않기 위한 안전장치]
2026-09-24 부터 표·뷰 구조는 전부 이 Alembic 이 만든다(data-pipeline 은 적재만).
upa_* 5종은 0028, mart 스키마 뷰는 0029 가 raw SQL(alembic/sql/)로 만든다.
그런데 이들은 SQLAlchemy 모델(Base.metadata)에는 **없다** — 그래서 아래 방어는
그대로 필요하다.

Alembic autogenerate는 기본적으로 "DB에는 있는데 target_metadata(=SQLAlchemy
모델)에는 없는" 테이블/인덱스를 삭제 대상으로 판단한다. 그대로 두면
`alembic revision --autogenerate` 한 번에 upa_port_call(20,000+행) 등에 대한
op.drop_table()이 마이그레이션 파일로 생성된다 — 리뷰에서 놓치면 실데이터가
통째로 날아간다(2026-08-11 alembic check 실행 중 실제로 재현됨).

그래서 화이트리스트 방식을 쓴다 — Base.metadata에 선언된 테이블만 비교
대상으로 삼고, 그 밖의 반영(reflected) 객체는 전부 무시한다.

mart 스키마는 include_schemas 기본값(False)이라 애초에 반영되지 않지만,
누군가 include_schemas=True로 바꿀 때를 대비해 여기서도 명시적으로 거른다.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "schema":
        return name in (None, "public")
    if type_ in ("table", "index") and reflected and compare_to is None:
        # DB에는 있지만 우리 모델엔 없는 객체 = data-pipeline 소유 or 정체불명.
        # 삭제 제안 대상에서 뺀다 (op.drop_table 사고 예방).
        return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_object=include_object,
        include_schemas=False,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        include_schemas=False,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
