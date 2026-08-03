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


_API_DESCRIPTION = """
울산항·온산항 액체화물 관제 지원 API.

### 화면별로 어디를 쓸지

| 만들려는 화면 | 쓸 API |
|---|---|
| **선석 배정 판정** | `POST /orchestrator/assess` — 기상·후보·혼재를 묶어 결론 하나 |
| **안전 챗봇** | `POST /rag/query` |
| **모니터링 대시보드** | `GET /dashboard/*` |
| 선석 후보 목록만 | `POST /scheduling/candidates` |
| 기상 판정만 | `POST /weather/assess` |
| 혼재 판정만 | `POST /safety/assess` |

**개별 에이전트 API를 각각 호출해 프론트에서 합치지 마세요.** 판단 순서와 조기 종료
조건이 `/orchestrator/assess` 안에 들어 있습니다. 예를 들어 기상이 불가면 선석 탐색을
아예 하지 않고, 선석이 정해진 뒤에는 그 선석의 부두그룹 임계값으로 기상을 다시
판정합니다.

### 공통 규칙

- 모든 시각은 **UTC**입니다 (`2026-08-04T09:00:00Z`)
- 위험등급·상태는 **한글 문자열**입니다 (`"안전"`, `"배정불가"`, `"하역중단"`).
  코드에서 비교할 때 이 문자열을 그대로 쓰세요
- **"판정 불가"를 "안전"으로 읽으면 안 됩니다.** 데이터가 없어 판단하지 못한 상태는
  등급이 낮은 게 아니라 가장 보수적으로 다뤄야 합니다
- 에러: `404` 미등재 화물 · `422` 요청 오류 또는 판정 불가 조건 · `502` 외부 API·LLM
  실패(재시도 가능) · `503` 인덱스 미적재
"""

_TAGS_METADATA = [
    {
        "name": "orchestrator",
        "description":
            "**선석 배정 화면의 주 엔드포인트.** 기상 → 선석 후보 → 선석별 기상 재판정 → "
            "혼재 판정을 순서대로 돌려 `overall_decision` 하나로 답합니다. "
            "`승인가능` 외 네 상태는 모두 '지금 하역하면 안 되는' 상태이며, 관제사가 원인을 "
            "구분할 수 있도록 분리해 뒀습니다.",
    },
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
        "name": "scheduling",
        "description":
            "선석 후보 탐색. LLM을 쓰지 않는 결정적 판단입니다.\n\n"
            "`berth_group`이 `null`인 것을 '온산 밖'으로 읽지 마세요 — 그 필드는 '기상 "
            "임계값 자료가 있는가'입니다. 온산 여부는 `onsan_scope`로 판단합니다.",
    },
    {
        "name": "weather",
        "description":
            "기상 기반 하역 가능 여부 판정. LLM을 쓰지 않는 임계값 룰엔진입니다.\n\n"
            "임계값이 **부두그룹별**이라 `berth_group`을 보내야 '같은 기상, 선석마다 다른 "
            "판정'이 동작합니다. 생략하면 전역 폴백 값을 씁니다.",
    },
    {
        "name": "safety",
        "description":
            "화물 혼재 위험 판정. MSDS 텍스트 기반 혼재금지와 IMDG 공인 격리표를 함께 "
            "봅니다.\n\n"
            "`rule_engine_floor`는 그래프 탐색으로 계산한 **결정적 하한**이고 LLM은 이보다 "
            "낮출 수 없습니다. `risk_level`과 함께 노출하면 '규칙이 정한 최소 등급'과 "
            "'AI가 추가로 본 위험'을 구분할 수 있습니다.",
    },
    {
        "name": "dashboard",
        "description": "모니터링 대시보드용 현황 조회. 판정 로직 없이 적재된 데이터를 그대로 냅니다.",
    },
    {
        "name": "chatbot",
        "description":
            "챗봇 화면 구성용 목록 조회. 질의응답은 `rag` 태그를 쓰세요.\n\n"
            "목록 출처가 PostgreSQL이 아니라 Neo4j인 것은 의도적입니다 — MSDS 원문만 있고 "
            "그래프에 없는 화물은 혼재금지 질문에 답할 수 없으므로, 사용자에게 보여줄 것은 "
            "'물어보면 답이 나오는 목록'입니다.",
    },
    {
        "name": "msds",
        "description":
            "MSDS 원문 조회. 16개 섹션 전체가 들어 있어 응답이 큽니다.\n\n"
            "DB에 없는 CAS번호는 KOSHA API로 실시간 조회 후 저장합니다(첫 호출은 느림). "
            "이렇게 들어온 화물은 지식그래프에 없어 혼재 판정을 할 수 없습니다.",
    },
    {"name": "health", "description": "서비스 상태 확인."},
]

app = FastAPI(
    title="Smart Port Backend",
    version="0.1.0",
    description=_API_DESCRIPTION,
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
