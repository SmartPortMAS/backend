"""create msds_embedding table (pgvector)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-26

MSDS 안전 챗봇(app/agents/chatbot)의 RAG 인덱스. msds_chemical.msds_payload에서
파생된 캐시 테이블이므로 원본이 갱신되면 backend/scripts/embed_msds.py를 다시
돌려야 한다(content_hash 비교로 변경분만 재임베딩).

인덱스 선택 — ivfflat 대신 HNSW:
  대상이 34종 × 섹션 청크 = 수백 행 규모라 ivfflat의 lists 튜닝(권장 rows/1000)은
  의미가 없고, 오히려 학습된 클러스터가 적어 recall이 떨어진다. HNSW는 사전 학습
  없이 빈 테이블에 만들어도 되고 이 규모에서 사실상 exact recall을 준다.
  vector_cosine_ops — OpenAI 임베딩은 정규화되어 있어 코사인 거리가 표준이다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 1536


def upgrade() -> None:
    # docker-compose의 postgres 이미지가 pgvector/pgvector:pg16 이라 확장이 동봉되어 있다.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "msds_embedding",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "chem_id",
            sa.String(length=20),
            sa.ForeignKey("msds_chemical.chem_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_kind", sa.String(length=20), nullable=False),
        sa.Column("section_key", sa.String(length=20), nullable=False),
        sa.Column("section_label", sa.String(length=100), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chem_id", "section_key", "chunk_index", name="uq_msds_embedding_chunk"),
    )

    op.create_index("idx_msds_embedding_chem_id", "msds_embedding", ["chem_id"])
    op.create_index("idx_msds_embedding_chunk_kind", "msds_embedding", ["chunk_kind"])
    op.execute(
        "CREATE INDEX idx_msds_embedding_vec ON msds_embedding "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index("idx_msds_embedding_vec", table_name="msds_embedding")
    op.drop_index("idx_msds_embedding_chunk_kind", table_name="msds_embedding")
    op.drop_index("idx_msds_embedding_chem_id", table_name="msds_embedding")
    op.drop_table("msds_embedding")
    # vector 확장은 다른 테이블이 쓸 수 있으므로 드롭하지 않는다.
