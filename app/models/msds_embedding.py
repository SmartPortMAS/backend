from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.llm.embeddings import EMBEDDING_DIM
from app.models.base import Base

# chunk_kind 값. 두 종류를 한 테이블에 두되 검색 시 반드시 필터해서 쓴다 —
# 물질명 해석(identity)과 근거 문단 검색(section)은 목적이 달라 섞이면 정확도가 떨어진다.
CHUNK_KIND_IDENTITY = "identity"  # 물질명/별칭/CAS/UN — 사용자 입력 물질명 → chem_id 해석용
CHUNK_KIND_SECTION = "section"  # MSDS detail 섹션 본문 — LLM 근거 컨텍스트용


class MsdsEmbedding(Base):
    """MSDS 텍스트 청크의 임베딩 벡터. 챗봇(app/agents/chatbot) RAG 인덱스.

    원본은 msds_chemical.msds_payload이고 이 테이블은 파생 캐시다. 따라서 스키마
    소유권은 backend(Alembic)에 있고, 적재는 backend/scripts/embed_msds.py가
    전담한다(data-pipeline이 아니다 — 임베딩 모델 선택이 챗봇 조회 경로와 한 몸이라
    같은 코드베이스에서 관리해야 벡터 공간 불일치를 막을 수 있다).
    """

    __tablename__ = "msds_embedding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chem_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("msds_chemical.chem_id", ondelete="CASCADE"), nullable=False
    )
    chunk_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    section_key: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="detail01~detail16, identity 청크는 'identity'"
    )
    section_label: Mapped[str] = mapped_column(String(100), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="content의 sha256. 재실행 시 변경분만 재임베딩"
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "chem_id", "section_key", "chunk_index", name="uq_msds_embedding_chunk"
        ),
        Index("idx_msds_embedding_chem_id", "chem_id"),
        Index("idx_msds_embedding_chunk_kind", "chunk_kind"),
    )
