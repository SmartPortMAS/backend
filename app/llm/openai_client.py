"""OpenAI 교체용 자리표시자(placeholder) 구현체.

LLM_PROVIDER=openai 로 전환할 때 이 클래스만 구현하면 된다. app.llm.base.LLMClient
계약(generate_structured)을 그대로 만족시키면, 에이전트 코드(app/agents/*)는
전혀 수정할 필요가 없다 — 이것이 인터페이스 분리의 목적이다.

구현 시 참고: OpenAI Responses API의 구조화 출력(response_format /
text.format=json_schema)을 사용해 Pydantic 스키마를 그대로 넘기면
Gemini 구현과 동일한 흐름으로 맞출 수 있다.
"""

from app.llm.base import LLMClient, SchemaT


class OpenAiClient(LLMClient):
    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[SchemaT],
    ) -> SchemaT:
        raise NotImplementedError(
            "OpenAI LLMClient는 아직 구현되지 않았습니다. "
            "app/llm/openai_client.py의 GeminiClient(app/llm/gemini_client.py)와 "
            "동일한 계약을 구현하세요."
        )
