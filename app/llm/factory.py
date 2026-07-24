from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLMClient
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
