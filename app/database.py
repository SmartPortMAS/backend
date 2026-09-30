from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

# JIT 끔: mart 뷰 조인의 행 수 과대추정(실제 1.7천 → 추정 477만)으로 비용이 JIT 기준을
# 넘어, 0.2초 쿼리에 LLVM 컴파일 3초가 붙었다(2026-09-30 로컬 PG16 실측). RDS 는 기본이 off.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"server_settings": {"jit": "off"}},
)
AsyncSessionFactory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionFactory() as session:
        yield session
