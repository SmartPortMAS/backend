"""통합 테스트 공용 픽스처 — 로컬 개발 DB(Postgres·Neo4j)를 그대로 쓴다.

앱의 전역 엔진은 커넥션 풀을 쓰는데, pytest-asyncio 는 테스트마다 이벤트 루프를
새로 만든다. 풀에 남은 커넥션이 이전 루프에 묶여 있어 다음 테스트에서 깨지므로
테스트용으로는 풀 없는 엔진과 테스트마다 새 드라이버를 쓴다.
"""

import pytest_asyncio
from neo4j import AsyncGraphDatabase
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def neo4j():
    s = get_settings()
    driver = AsyncGraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    yield driver
    await driver.close()
