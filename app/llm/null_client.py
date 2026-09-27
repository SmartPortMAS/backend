"""LLM 을 부르지 않는 LLMClient — 서술 필드를 빈 값으로 채운다.

에이전트의 등급·근거는 규칙으로 정해지고 LLM 은 문장만 쓴다. 문장이 필요 없는 호출
(판정 잡이 '결과가 바뀌었는가'만 먼저 볼 때)에 넘겨 LLM 비용 없이 같은 판정을 얻는다.
"""

from typing import get_origin

from .base import LLMClient, SchemaT


class NullLLMClient(LLMClient):
    async def generate_structured(self, *, system_prompt: str, user_prompt: str, schema: type[SchemaT]) -> SchemaT:
        empty = {
            name: [] if get_origin(field.annotation) is list else ""
            for name, field in schema.model_fields.items()
        }
        return schema(**empty)
