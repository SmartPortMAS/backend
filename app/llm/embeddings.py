"""임베딩 프로바이더 인터페이스 및 구현체.

app.llm.base.LLMClient(생성)와 같은 이유로 분리한다 — 챗봇 코드는 이 인터페이스에만
의존하고, 프로바이더 교체는 app.llm.factory.get_embedding_client()의 반환 구현체만
바꾸면 된다.

EMBEDDING_DIM은 pgvector 컬럼 차원(alembic 0006)과 반드시 일치해야 한다. 임베딩
모델을 바꿔 차원이 달라지면 마이그레이션과 전체 재적재가 함께 필요하다.
"""

from abc import ABC, abstractmethod

from openai import AsyncOpenAI

from app.core.exceptions import EmbeddingGenerationError

# text-embedding-3-small / -large(dimensions=1536 축소) 모두 이 차원을 쓴다.
EMBEDDING_DIM = 1536


class EmbeddingClient(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """텍스트 목록을 같은 순서의 임베딩 벡터 목록으로 변환한다."""
        raise NotImplementedError

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0]


class OpenAiEmbeddingClient(EmbeddingClient):
    def __init__(self, *, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=texts,
                dimensions=EMBEDDING_DIM,
            )
        except Exception as e:  # noqa: BLE001 - 업스트림 SDK 예외를 도메인 예외로 통일
            raise EmbeddingGenerationError(provider="openai", reason=str(e)) from e

        # API는 index 순서를 보장하지만, 배치 응답 순서에 의존하는 버그는 조용히
        # 잘못된 벡터를 저장해버리므로 명시적으로 정렬한다.
        return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]
