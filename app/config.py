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
    embedding_model: str = "text-embedding-3-small"
    kma_api_key: str | None = None
    kosha_api_key: str | None = None
    port_mis_api_key: str | None = None
    mof_api_key: str | None = None
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
