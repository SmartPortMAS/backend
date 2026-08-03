from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.llm.base import LLMClient
from app.llm.embeddings import EmbeddingClient
from app.llm.factory import get_embedding_client as _get_embedding_client
from app.llm.factory import get_llm_client as _get_llm_client


async def get_session(session: AsyncSession = Depends(get_db)) -> AsyncSession:
    return session


def get_llm_client() -> LLMClient:
    return _get_llm_client()


def get_embedding_client() -> EmbeddingClient:
    return _get_embedding_client()
