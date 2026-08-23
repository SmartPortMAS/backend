"""OpenAI API 기반 LLMClient 구현체.

OpenAI Python SDK(https://pypi.org/project/openai/)의 Responses API 구조화
출력(client.responses.parse(text_format=...))으로 Pydantic 스키마를 그대로
강제한다. GeminiClient(app/llm/gemini_client.py)와 동일한 흐름 —
app.llm.base.LLMClient 계약만 만족하므로 에이전트 코드(app/agents/*)는
provider 전환 시 한 줄도 수정할 필요가 없다.
"""

import asyncio
import logging

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.core.exceptions import LLMGenerationError
from app.llm.base import LLMClient, SchemaT

logger = logging.getLogger(__name__)

# 재시도 정책 (2026-08-22 성능 측정에서 추가).
#
# 왜 필요한가: `POST /api/v1/rag/query` 53회 실측에서 4회(7.5%)가
# "output_parsed is None"으로 502가 났다. 업스트림은 HTTP 200을 주는데
# 구조화 출력만 비어 돌아오는 간헐적 현상이라, 같은 요청을 그대로 다시
# 보내면 대부분 성공한다(같은 프롬프트를 인프로세스로 12회 돌렸을 때는
# 0회 실패 — 결정적 원인이 아니라는 뜻).
#
# 예외(네트워크·레이트리밋)와 파싱 실패를 **같이** 재시도한다. 파싱 실패는
# 예외가 아니라 정상 응답 안의 빈 결과라, 예외만 잡는 재시도로는 못 막는다.
# 지수 백오프로 업스트림 순간 장애에 몰려가지 않게 한다.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5


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
        last_reason = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.responses.parse(
                    model=self._model,
                    instructions=system_prompt,
                    input=user_prompt,
                    text_format=schema,
                )
            except Exception as e:  # noqa: BLE001 - 업스트림 SDK 예외를 하나의 도메인 예외로 통일
                last_reason = str(e)
            else:
                parsed = response.output_parsed
                if parsed is not None and isinstance(parsed, BaseModel):
                    return parsed
                last_reason = (
                    "응답을 스키마로 파싱하지 못했습니다 "
                    f"(response.output_parsed is None, status={response.status})."
                )

            if attempt < _MAX_ATTEMPTS:
                delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "openai generate_structured 실패 (%d/%d, schema=%s): %s — %.1fs 후 재시도",
                    attempt, _MAX_ATTEMPTS, schema.__name__, last_reason, delay,
                )
                await asyncio.sleep(delay)

        raise LLMGenerationError(
            provider="openai",
            reason=f"{_MAX_ATTEMPTS}회 시도 후에도 실패했습니다: {last_reason}",
        )
