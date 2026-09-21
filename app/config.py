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
    # 하역 개시 인터락 게이트(라즈베리파이) — MQTT 브로커(노트북 mosquitto)와 게이트→선석 대응.
    # 시연 장치는 G01 = 선석 A, G02 = 선석 B. 선석은 마스터 표기(upa_berth_facility.wharf_name).
    # 기본값은 풍속 중단 기준이 다른 두 부두(OTK1 14 m/s · 정일1 17 m/s)라 풍속 16 으로
    # A 잠김·B 열림을 보일 수 있다(하드웨어/UI연동_전달사항_20260922.md 5절).
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    gate_berths: str = '{"G01": "OTK1부두", "G02": "정일1부두"}'
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
