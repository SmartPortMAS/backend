from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: Literal["development", "production"] = Field(default="development")
    database_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    llm_provider: Literal["gemini", "openai"] = "gemini"
    llm_model: str = "gemini-flash-latest"
    # 임베딩은 llm_provider와 독립적으로 고른다. 챗봇 RAG의 벡터 공간은 적재
    # 시점(scripts/embed_msds.py)과 조회 시점이 반드시 같은 모델이어야 하는데,
    # llm_provider는 답변 품질/비용 사정으로 자유롭게 바뀔 수 있기 때문이다.
    embedding_provider: Literal["openai"] = "openai"
    # 기본값을 바꾸면 적재된 벡터와 공간이 어긋나므로 반드시 --rebuild를 함께 돌려야
    # 한다. -large를 쓰되 dimensions=1536으로 축소해 받는다(EMBEDDING_DIM) — pgvector
    # HNSW 인덱스가 2000차원까지만 지원해서 -large 원본 3072차원은 색인할 수 없다.
    # -small 대비 실측(80문항): 정답 섹션 1위 적중 +20.7%p. 06번 설계문서 3-5절 참고.
    embedding_model: str = "text-embedding-3-large"
    kma_api_key: str | None = None
    kosha_api_key: str | None = None
    port_mis_api_key: str | None = None
    mof_api_key: str | None = None
    # 국립해양조사원 조석예보 키는 여기 없다 — 백엔드가 그 API 를 직접 부르지 않는다.
    # data-pipeline 이 받아 tide_forecast 에 적재하고, 백엔드는 그 표를 읽는다
    # (api/v1/twin.py · services/tide.py). 키는 data-pipeline/.env 의 KHOA_API_KEY.
    log_level: str = Field(default="INFO")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
