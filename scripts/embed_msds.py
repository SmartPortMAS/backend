"""MSDS 임베딩 인덱스 적재 — msds_chemical(JSONB) → msds_embedding(pgvector).

실행:
    cd backend
    .venv/Scripts/python -m scripts.embed_msds            # 변경분만 재임베딩
    .venv/Scripts/python -m scripts.embed_msds --rebuild  # 전량 삭제 후 재적재

멱등성: 청크 본문의 sha256(content_hash)을 DB 값과 비교해 달라진 청크만 임베딩을
다시 호출한다. MSDS 원문이 그대로면 두 번째 실행은 OpenAI API를 한 번도 부르지
않는다. 청크 규칙(app/agents/chatbot/chunking.py)이나 embedding_model을 바꾼
경우에는 --rebuild가 필요하다 — 벡터 공간이 달라지면 기존 벡터와 섞어 쓸 수 없다.

data-pipeline이 아니라 backend에 둔 이유는 app/models/msds_embedding.py 주석 참고.
"""

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agents.chatbot.chunking import build_chunks, content_hash
from app.config import get_settings
from app.core.logging import configure_logging
from app.database import AsyncSessionFactory
from app.llm.factory import get_embedding_client
from app.models import MsdsChemical, MsdsEmbedding

logger = logging.getLogger("embed_msds")

# OpenAI 임베딩 API 1회 호출당 청크 수. 34종 × 평균 10청크 ≈ 350청크라
# 64면 6회 호출로 끝난다.
EMBED_BATCH_SIZE = 64


async def _existing_hashes(db) -> dict[tuple[str, str, int], str]:
    rows = await db.execute(
        select(
            MsdsEmbedding.chem_id,
            MsdsEmbedding.section_key,
            MsdsEmbedding.chunk_index,
            MsdsEmbedding.content_hash,
        )
    )
    return {(r.chem_id, r.section_key, r.chunk_index): r.content_hash for r in rows}


async def run(rebuild: bool) -> None:
    settings = get_settings()
    embedding_client = get_embedding_client()

    async with AsyncSessionFactory() as db:
        if rebuild:
            deleted = await db.execute(delete(MsdsEmbedding))
            await db.commit()
            logger.info("--rebuild: 기존 임베딩 %s건 삭제", deleted.rowcount)

        rows = list(
            await db.scalars(
                select(MsdsChemical)
                .where(MsdsChemical.quality_flag == "OK")
                .order_by(MsdsChemical.chem_id)
            )
        )
        logger.info("대상 화학물질 %d종", len(rows))

        existing = {} if rebuild else await _existing_hashes(db)

        all_chunks: list[dict] = [
            {**chunk, "chem_id": row.chem_id, "content_hash": content_hash(chunk["content"])}
            for row in rows
            for chunk in build_chunks(row)
        ]

        pending = [
            chunk
            for chunk in all_chunks
            if existing.get((chunk["chem_id"], chunk["section_key"], chunk["chunk_index"]))
            != chunk["content_hash"]
        ]
        skipped = len(all_chunks) - len(pending)

        logger.info("임베딩 대상 %d청크 (변경 없음 %d청크 건너뜀)", len(pending), skipped)
        if not pending:
            # 삭제된 청크 정리는 아래에서 계속 수행해야 하므로 return하지 않는다.
            logger.info("새로 임베딩할 청크가 없습니다")

        now = datetime.now(tz=timezone.utc)
        for start in range(0, len(pending), EMBED_BATCH_SIZE):
            batch = pending[start : start + EMBED_BATCH_SIZE]
            vectors = await embedding_client.embed([c["content"] for c in batch])

            stmt = pg_insert(MsdsEmbedding).values(
                [
                    {
                        "chem_id": chunk["chem_id"],
                        "chunk_kind": chunk["chunk_kind"],
                        "section_key": chunk["section_key"],
                        "section_label": chunk["section_label"],
                        "chunk_index": chunk["chunk_index"],
                        "content": chunk["content"],
                        "content_hash": chunk["content_hash"],
                        "embedding": vector,
                        "embedding_model": settings.embedding_model,
                        "created_at": now,
                    }
                    for chunk, vector in zip(batch, vectors)
                ]
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_msds_embedding_chunk",
                    set_={
                        "chunk_kind": stmt.excluded.chunk_kind,
                        "section_label": stmt.excluded.section_label,
                        "content": stmt.excluded.content,
                        "content_hash": stmt.excluded.content_hash,
                        "embedding": stmt.excluded.embedding,
                        "embedding_model": stmt.excluded.embedding_model,
                        "created_at": stmt.excluded.created_at,
                    },
                )
            )
            await db.commit()
            logger.info("적재 %d/%d", min(start + EMBED_BATCH_SIZE, len(pending)), len(pending))

        # 원문에서 사라진 청크(섹션이 비게 되거나 청크 수가 줄어든 경우) 정리.
        # 남겨두면 오래된 문구가 검색 결과에 계속 잡혀 답변 근거를 오염시킨다.
        valid_keys = {
            (chunk["chem_id"], chunk["section_key"], chunk["chunk_index"]) for chunk in all_chunks
        }
        stale = [key for key in (await _existing_hashes(db)) if key not in valid_keys]
        for chem_id, section_key, chunk_index in stale:
            await db.execute(
                delete(MsdsEmbedding).where(
                    MsdsEmbedding.chem_id == chem_id,
                    MsdsEmbedding.section_key == section_key,
                    MsdsEmbedding.chunk_index == chunk_index,
                )
            )
        if stale:
            await db.commit()
            logger.info("사라진 청크 %d건 삭제", len(stale))

        total = await db.scalar(select(func.count()).select_from(MsdsEmbedding))
        logger.info("완료 — msds_embedding 총 %s행 (모델: %s)", total, settings.embedding_model)


def main() -> None:
    parser = argparse.ArgumentParser(description="MSDS 임베딩 인덱스 적재")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="기존 임베딩을 전부 삭제하고 재적재 (청크 규칙/임베딩 모델 변경 시 필수)",
    )
    args = parser.parse_args()

    configure_logging(get_settings().log_level)
    asyncio.run(run(args.rebuild))


if __name__ == "__main__":
    main()
