from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.health import router as health_router
from app.api.v1.msds import router as msds_router
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


app = FastAPI(title="Smart Port Backend", version="0.1.0", lifespan=lifespan)

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


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
