from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.chatbot import router as chatbot_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.health import router as health_router
from app.api.v1.msds import router as msds_router
from app.api.v1.orchestrator import router as orchestrator_router
from app.api.v1.rag import router as rag_router
from app.api.v1.safety import router as safety_router
from app.api.v1.scheduling import router as scheduling_router
from app.api.v1.weather import router as weather_router
from app.config import get_settings
from app.core.logging import configure_logging
from app.neo4j_client import neo4j_client

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await neo4j_client.driver.verify_connectivity()
    yield
    await neo4j_client.close()


_TAGS_METADATA = [
    {
        "name": "rag",
        "description":
            "MSDS 안전 질의응답. 자연어 질문을 받아 MSDS 원문·지식그래프·정형 값을 "
            "근거로 답합니다.\n\n"
            "**프론트엔드가 먼저 볼 것** — `POST /rag/query`의 상세 설명에 응답 읽는 법이 "
            "정리돼 있습니다. 특히 `citations[].score`가 `null`이면 확정값이고, "
            "`unresolved`가 비어 있지 않으면 경고를 띄워야 합니다.",
    },
    {
        "name": "chatbot",
        "description":
            "챗봇 화면 구성용 목록 조회. 질의응답은 `rag` 태그를 쓰세요.\n\n"
            "목록 출처가 PostgreSQL이 아니라 Neo4j인 것은 의도적입니다 — MSDS 원문만 있고 "
            "그래프에 없는 화물은 혼재금지 질문에 답할 수 없으므로, 사용자에게 보여줄 것은 "
            "'물어보면 답이 나오는 목록'입니다.",
    },
]

app = FastAPI(
    title="Smart Port Backend",
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=_TAGS_METADATA,
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


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
