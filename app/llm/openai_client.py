"""OpenAI API 기반 LLMClient 구현체.

OpenAI Python SDK(https://pypi.org/project/openai/)의 Responses API 구조화
출력(client.responses.parse(text_format=...))으로 Pydantic 스키마를 그대로
강제한다. GeminiClient(app/llm/gemini_client.py)와 동일한 흐름 —
app.llm.base.LLMClient 계약만 만족하므로 에이전트 코드(app/agents/*)는
provider 전환 시 한 줄도 수정할 필요가 없다.
"""

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.core.exceptions import LLMGenerationError
from app.llm.base import LLMClient, SchemaT


class OpenAiClient(LLMClient):
    def __init__(self, *, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[SchemaT],
    ) -> SchemaT:
        try:
            response = await self._client.responses.parse(
                model=self._model,
                instructions=system_prompt,
                input=user_prompt,
                text_format=schema,
            )
        except Exception as e:  # noqa: BLE001 - 업스트림 SDK 예외를 하나의 도메인 예외로 통일
            raise LLMGenerationError(provider="openai", reason=str(e)) from e

        parsed = response.output_parsed
        if parsed is None or not isinstance(parsed, BaseModel):
            raise LLMGenerationError(
                provider="openai",
                reason="응답을 스키마로 파싱하지 못했습니다 (response.output_parsed is None).",
            )
        return parsed
