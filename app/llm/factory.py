from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLMClient
from app.llm.embeddings import EmbeddingClient, OpenAiEmbeddingClient
from app.llm.gemini_client import GeminiClient
from app.llm.openai_client import OpenAiClient


@lru_cache
def get_llm_client() -> LLMClient:
    """settings.llm_provider 값에 따라 LLMClient 구현체를 고른다.

    provider를 바꾸는 것만으로 구현체가 전환되며, 호출부(app/agents/*)는
    반환 타입이 LLMClient라는 것 외에는 아무것도 알 필요가 없다.
    """
    settings = get_settings()

    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다 (.env 확인).")
        return GeminiClient(api_key=settings.gemini_api_key, model=settings.llm_model)

    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다 (.env 확인).")
        return OpenAiClient(api_key=settings.openai_api_key, model=settings.llm_model)

    raise ValueError(f"지원하지 않는 llm_provider: {settings.llm_provider}")


@lru_cache
def get_chatbot_llm_client() -> LLMClient:
    """챗봇용 LLMClient. settings.chatbot_llm_model 이 있으면 그 모델, 없으면 llm_model.

    provider 는 get_llm_client 와 같다 — 챗봇만 다른 provider 를 쓸 이유가 아직 없다.
    """
    settings = get_settings()
    return _client_for(settings.chatbot_llm_model or settings.llm_model)


@lru_cache
def get_safety_llm_client() -> LLMClient:
    """화물 혼재 심사(POST /safety/assess)용 LLMClient. safety_llm_model 이 있으면 그 모델, 없으면 llm_model.

    [2026-09-29] 종합 문장 품질 때문에 llm_model 을 mini 로 올리자 혼재 심사 화면(화물 특성·체크리스트만
    LLM)이 느려졌다 — 등급은 규칙엔진이 정하므로 이 서술은 작은 모델로 충분하다.
    """
    settings = get_settings()
    return _client_for(settings.safety_llm_model or settings.llm_model)


def _client_for(model: str) -> LLMClient:
    settings = get_settings()
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다 (.env 확인).")
        return GeminiClient(api_key=settings.gemini_api_key, model=model)

    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다 (.env 확인).")
        return OpenAiClient(api_key=settings.openai_api_key, model=model)

    raise ValueError(f"지원하지 않는 llm_provider: {settings.llm_provider}")


@lru_cache
def get_embedding_client() -> EmbeddingClient:
    """settings.embedding_provider 값에 따라 EmbeddingClient 구현체를 고른다.

    llm_provider와 별개로 관리한다(app/config.py 주석 참고). 챗봇 RAG 인덱스는
    적재/조회가 동일 모델이어야 하므로 provider 전환은 재적재를 동반해야 한다.
    """
    settings = get_settings()

    if settings.embedding_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다 (.env 확인).")
        return OpenAiEmbeddingClient(
            api_key=settings.openai_api_key, model=settings.embedding_model
        )

    raise ValueError(f"지원하지 않는 embedding_provider: {settings.embedding_provider}")
