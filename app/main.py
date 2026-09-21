import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from sqlalchemy import text

from app.api.v1.approvals import router as approvals_router
from app.api.v1.arrivals import router as arrivals_router
from app.api.v1.chatbot import router as chatbot_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.hardware import router as hardware_router
from app.api.v1.health import router as health_router
from app.api.v1.msds import router as msds_router
from app.api.v1.orchestrator import router as orchestrator_router
from app.api.v1.rag import router as rag_router
from app.api.v1.safety import router as safety_router
from app.api.v1.scheduling import router as scheduling_router
from app.api.v1.twin import router as twin_router
from app.api.v1.weather import router as weather_router
from app.config import get_settings
from app.core.logging import configure_logging
from app.database import AsyncSessionFactory
from app.neo4j_client import neo4j_client
from app.scheduler import create_scheduler

settings = get_settings()
configure_logging(settings.log_level)


async def _warm_up_postgres() -> None:
    """PostgreSQL 커넥션 풀을 미리 하나 채워 둔다 (2026-08-22 성능 측정).

    asyncpg 풀은 지연 생성이라 첫 요청이 커넥션 수립 비용을 혼자 문다 —
    기상분석 에이전트 실측에서 1회차만 2,102ms, 2회차부터 21.5ms였다(약 100배).
    관제사가 화면을 처음 열었을 때가 정확히 그 1회차라, 기동 시점으로 옮긴다.

    실패해도 앱을 막지 않는다. 이건 최적화지 기능 요건이 아니고, DB가 잠깐
    늦게 뜨는 개발 환경에서 API 전체가 안 뜨는 쪽이 훨씬 나쁘다.
    """
    try:
        async with AsyncSessionFactory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - 워밍업 실패는 기동을 막지 않는다
        logging.getLogger(__name__).warning("PostgreSQL 워밍업 실패 — 첫 요청이 느릴 수 있습니다", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await neo4j_client.driver.verify_connectivity()
    await _warm_up_postgres()
    # 08_스케줄링_전면재설계_자동배정_설계문서.md §5.1·§5.5 — 입항 자동추천·출항
    # 자동해제 백그라운드 잡. 둘 다 실패해도 API 자체는 계속 떠 있어야 하므로
    # 앱 시작을 막지 않는다(스케줄러 자체 등록 실패만 여기서 전파됨).
    scheduler = create_scheduler()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    await neo4j_client.close()


_API_DESCRIPTION = """
울산항·온산항 액체화물 관제 지원 API.

| 만들려는 화면 | 쓸 API |
|---|---|
| 선석 배정 판정 | `POST /orchestrator/assess` |
| 안전 챗봇 | `POST /rag/query` |
| 모니터링 대시보드 | `GET /dashboard/*` |

개별 에이전트 API(`/scheduling` `/weather` `/safety`)를 각각 호출해 프론트에서 합치지
마세요. 판단 순서와 조기 종료 조건이 `/orchestrator/assess` 안에 있습니다.

**공통 규칙**

- 시각은 UTC (`2026-08-04T09:00:00Z`)
- 위험등급·상태는 한글 문자열 (`"안전"`, `"하역불가"`, `"하역중단"`)
- **"판정 불가"는 "안전"이 아닙니다.** 데이터가 없어 판단하지 못한 상태이므로 가장
  보수적으로 다루세요
- 에러: `404` 미등재 화물 · `422` 요청 오류 · `502` 외부 API·LLM 실패(재시도 가능) ·
  `503` 인덱스 미적재
"""

_TAGS_METADATA = [
    {"name": "orchestrator", "description": "선석 배정 종합 판정. **배정 화면의 주 엔드포인트.**"},
    {"name": "rag", "description": "MSDS 안전 질의응답 (챗봇)."},
    {"name": "scheduling", "description": "선석 후보 탐색. LLM 미사용."},
    {"name": "weather", "description": "기상 기반 하역 가능 여부 판정. LLM 미사용."},
    {"name": "safety", "description": "화물 혼재 위험 판정."},
    {"name": "dashboard", "description": "모니터링 현황 조회. 판정 로직 없음."},
    {"name": "approvals", "description": "관제사 사전승인. 시스템 자동추천(REQUESTED)을 승인/반려해야 실제 배정이 확정된다."},
    {"name": "chatbot", "description": "챗봇 화면용 목록 조회. 질의응답은 `rag`."},
    {"name": "msds", "description": "MSDS 원문 조회."},
    {"name": "health", "description": "서비스 상태 확인."},
]


def _operation_id(route: APIRoute) -> str:
    """`assess_api_v1_orchestrator_assess_post` 대신 `orchestrator_assess`로.

    FastAPI 기본값은 경로와 메서드를 이어붙여 길고 읽기 어렵다. 이 값은 Swagger UI에
    그대로 보이고, OpenAPI로 클라이언트를 생성하면 함수명이 되므로 짧게 유지한다.
    """
    tag = route.tags[0] if route.tags else "api"
    return route.name if route.name.startswith(tag) else f"{tag}_{route.name}"


app = FastAPI(
    title="Smart Port Backend",
    version="0.1.0",
    description=_API_DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=_TAGS_METADATA,
    generate_unique_id_function=_operation_id,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api/v1")
app.include_router(msds_router, prefix="/api/v1")
app.include_router(safety_router, prefix="/api/v1")
app.include_router(scheduling_router, prefix="/api/v1")
app.include_router(weather_router, prefix="/api/v1")
app.include_router(orchestrator_router, prefix="/api/v1")
app.include_router(dashboard_router, prefix="/api/v1")
app.include_router(chatbot_router, prefix="/api/v1")
app.include_router(rag_router, prefix="/api/v1")
app.include_router(twin_router, prefix="/api/v1")
app.include_router(approvals_router, prefix="/api/v1")
app.include_router(arrivals_router, prefix="/api/v1")
# 하드웨어 릴레이만 prefix 가 없다 — 프런트가 기다리는 경로가
# '/ws/hardware' 라서다(useHardwareData.js:4). 그 계약을 바꾸지 않는다.
app.include_router(hardware_router)


@app.get("/health", summary="서비스 상태 확인")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
