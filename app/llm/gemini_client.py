"""Gemini API 기반 LLMClient 구현체.

google-genai SDK(https://pypi.org/project/google-genai/)의 response_schema
기능으로 JSON 스키마 강제 출력을 사용한다. 이 모듈은 app.llm.base.LLMClient
계약만 만족하면 되므로, 다른 SDK 버전이나 모델로 바뀌어도 이 파일만 수정하면 된다.
"""

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.core.exceptions import LLMGenerationError
from app.llm.base import LLMClient, SchemaT


class GeminiClient(LLMClient):
    def __init__(self, *, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[SchemaT],
    ) -> SchemaT:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
        except Exception as e:  # noqa: BLE001 - 업스트림 SDK 예외를 하나의 도메인 예외로 통일
            raise LLMGenerationError(provider="gemini", reason=str(e)) from e

        parsed = response.parsed
        if parsed is None or not isinstance(parsed, BaseModel):
            raise LLMGenerationError(
                provider="gemini",
                reason="응답을 스키마로 파싱하지 못했습니다 (response.parsed is None).",
            )
        return parsed
