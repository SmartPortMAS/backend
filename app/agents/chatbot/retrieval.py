"""물질명 해석(entity resolution)과 MSDS 벡터 검색.

두 기능 모두 pgvector를 쓰지만 성격이 다르다:
- 물질명 해석: 결정적 매칭(CAS/정확명/별칭)이 우선이고 벡터는 최후 수단.
  임계값 미달이면 매칭하지 않고 "미등재"로 남긴다 — 안전 판정에서 엉뚱한 물질을
  자신 있게 매칭하는 것이 매칭 실패보다 훨씬 위험하다.
- 근거 검색: 순수 벡터 유사도. 필요하면 chem_id로 범위를 좁힌다.
"""

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EmbeddingIndexEmptyError
from app.llm.embeddings import EmbeddingClient
from app.models import CHUNK_KIND_IDENTITY, CHUNK_KIND_SECTION, MsdsChemical, MsdsEmbedding

from .aliases import ALIAS_TO_CAS, looks_like_cas, normalize_name
from .schemas import ChemicalMatch, MatchMethod, RetrievedChunk

# identity 청크 유사도 임계값. 이 아래는 매칭하지 않고 미등재로 처리한다.
# text-embedding-3-small에서 서로 다른 화학물질의 identity 청크끼리도 0.6~0.7대가
# 흔히 나오므로(전부 "화학물질명/CAS번호/UN번호" 같은 틀을 공유) 넉넉히 높게 잡았다.
IDENTITY_MATCH_THRESHOLD = 0.78
# 이 값을 넘으면 결정적 매칭에 준하는 신뢰도로 본다(confidence 계산에 사용).
IDENTITY_STRONG_THRESHOLD = 0.86

# 근거 청크 유사도 하한. 이보다 낮은 청크는 프롬프트 노이즈일 뿐이라 버린다.
CONTEXT_MIN_SCORE = 0.25


def _to_match(row: MsdsChemical, *, query_name: str, method: MatchMethod, score: float | None = None) -> ChemicalMatch:
    return ChemicalMatch(
        query_name=query_name,
        chem_id=row.chem_id,
        name_ko=row.name_ko,
        name_en=row.name_en,
        cas_no=row.cas_no,
        method=method,
        score=score,
    )


async def _load_name_index(db: AsyncSession) -> tuple[dict[str, MsdsChemical], dict[str, MsdsChemical]]:
    """(정규화 이름 → 행, CAS → 행) 인덱스. 34종 규모라 매 요청 전량 적재해도 무방하다."""
    rows = list(await db.scalars(select(MsdsChemical).where(MsdsChemical.quality_flag == "OK")))

    by_name: dict[str, MsdsChemical] = {}
    by_cas: dict[str, MsdsChemical] = {}
    for row in rows:
        for name in (row.name_ko, row.name_en):
            if name:
                by_name.setdefault(normalize_name(name), row)
        if row.cas_no:
            by_cas[row.cas_no.strip()] = row
    return by_name, by_cas


def _cosine_score(distance_column):
    """pgvector의 코사인 '거리'(0~2)를 유사도(1~-1)로 뒤집는다."""
    return 1 - distance_column


async def resolve_chemical_names(
    db: AsyncSession,
    embedding_client: EmbeddingClient,
    names: list[str],
) -> tuple[list[ChemicalMatch], list[str]]:
    """물질명 목록을 (해석 성공 목록, 해석 실패 목록)으로 나눈다.

    같은 화물이 두 이름으로 들어오면(예: "휘발유"와 "가솔린") chem_id 기준으로
    중복 제거한다.
    """
    if not names:
        return [], []

    by_name, by_cas = await _load_name_index(db)

    matches: list[ChemicalMatch] = []
    unresolved: list[str] = []
    pending_vector: list[str] = []

    for raw in names:
        candidate = raw.strip()
        if not candidate:
            continue

        if looks_like_cas(candidate) and candidate in by_cas:
            matches.append(_to_match(by_cas[candidate], query_name=raw, method=MatchMethod.CAS))
            continue

        normalized = normalize_name(candidate)
        if normalized in by_name:
            matches.append(_to_match(by_name[normalized], query_name=raw, method=MatchMethod.EXACT_NAME))
            continue

        alias_cas = ALIAS_TO_CAS.get(normalized)
        if alias_cas and alias_cas in by_cas:
            matches.append(_to_match(by_cas[alias_cas], query_name=raw, method=MatchMethod.ALIAS))
            continue

        pending_vector.append(raw)

    if pending_vector:
        vector_matches, vector_failed = await _resolve_by_vector(db, embedding_client, pending_vector)
        matches.extend(vector_matches)
        unresolved.extend(vector_failed)

    deduped: dict[str, ChemicalMatch] = {}
    for match in matches:
        deduped.setdefault(match.chem_id, match)
    return list(deduped.values()), unresolved


async def _resolve_by_vector(
    db: AsyncSession,
    embedding_client: EmbeddingClient,
    names: list[str],
) -> tuple[list[ChemicalMatch], list[str]]:
    if not await _index_has_rows(db):
        raise EmbeddingIndexEmptyError()

    vectors = await embedding_client.embed(names)

    matches: list[ChemicalMatch] = []
    unresolved: list[str] = []

    for raw, vector in zip(names, vectors):
        distance = MsdsEmbedding.embedding.cosine_distance(vector)
        stmt: Select = (
            select(MsdsChemical, _cosine_score(distance).label("score"))
            .join(MsdsEmbedding, MsdsEmbedding.chem_id == MsdsChemical.chem_id)
            .where(MsdsEmbedding.chunk_kind == CHUNK_KIND_IDENTITY)
            .order_by(distance)
            .limit(1)
        )
        row = (await db.execute(stmt)).first()
        if row is None or row.score < IDENTITY_MATCH_THRESHOLD:
            unresolved.append(raw)
            continue
        matches.append(
            _to_match(row[0], query_name=raw, method=MatchMethod.VECTOR, score=round(float(row.score), 4))
        )

    return matches, unresolved


async def _index_has_rows(db: AsyncSession) -> bool:
    return await db.scalar(select(MsdsEmbedding.id).limit(1)) is not None


async def search_context(
    db: AsyncSession,
    embedding_client: EmbeddingClient,
    question: str,
    *,
    top_k: int = 6,
    chem_ids: list[str] | None = None,
) -> list[RetrievedChunk]:
    """질문과 유사한 MSDS 섹션 청크를 반환한다.

    chem_ids가 주어지면 그 화물들로 범위를 좁힌다. 물질이 특정된 질문에서 전체
    코퍼스를 뒤지면 다른 화물의 비슷한 문구(어차피 MSDS는 정형 문장이라 서로
    매우 유사하다)가 섞여 들어와 답변이 오염된다.
    """
    if not await _index_has_rows(db):
        raise EmbeddingIndexEmptyError()

    vector = await embedding_client.embed_one(question)
    distance = MsdsEmbedding.embedding.cosine_distance(vector)

    stmt: Select = (
        select(
            MsdsEmbedding.chem_id,
            MsdsEmbedding.section_key,
            MsdsEmbedding.section_label,
            MsdsEmbedding.content,
            _cosine_score(distance).label("score"),
            MsdsChemical.name_ko,
            MsdsChemical.name_en,
            MsdsChemical.cas_no,
        )
        .join(MsdsChemical, MsdsChemical.chem_id == MsdsEmbedding.chem_id)
        .where(MsdsEmbedding.chunk_kind == CHUNK_KIND_SECTION)
        .order_by(distance)
        .limit(top_k)
    )
    if chem_ids:
        stmt = stmt.where(MsdsEmbedding.chem_id.in_(chem_ids))

    rows = (await db.execute(stmt)).all()
    return [
        RetrievedChunk(
            chem_id=row.chem_id,
            chemical_name=row.name_ko or row.name_en or row.chem_id,
            cas_no=row.cas_no,
            section_key=row.section_key,
            section_label=row.section_label,
            content=row.content,
            score=round(float(row.score), 4),
        )
        for row in rows
        if row.score >= CONTEXT_MIN_SCORE
    ]
