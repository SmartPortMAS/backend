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

# 질의를 identity 청크와 같은 형식으로 감싸서 임베딩한다.
#
# [2026-08-23] 이걸 안 하면 벡터 경로가 통째로 죽는다. 청크는 4줄 구조화 문서
# ("화학물질명: 벤젠 / CAS번호: … / UN번호: … / 이명·약칭: …")인데 질의는 "벤젠"
# 단어 하나라, 길이·형식 차이만으로 코사인 유사도가 크게 깎였다. 실측(36종 적재분,
# text-embedding-3-large):
#     "벤젠"                    -> 0.4523   (임계 0.78 미달 = 매칭 실패)
#     "화학물질명: 벤젠"          -> 0.8050
# 정확한 물질명조차 임계를 못 넘어, 지금까지 물질 해석은 전적으로 결정적 매칭
# (CAS/정확명/별칭 사전)에만 의존하고 있었다. evals/README.md도 "벡터 물질명 매칭
# 경로가 검증되지 않습니다"라고 적어 두었는데, 평가셋이 이 경로를 안 타서 죽은 걸
# 아무도 몰랐다.
#
# 한글이라 낮았던 게 아니다 — 영문("benzene")도 0.5723으로 비슷하게 낮았고,
# 한글 그대로 형식만 맞추자 0.8050으로 올랐다(개선폭이 영문 전환의 7배).
_IDENTITY_QUERY_TEMPLATE = "화학물질명: {}"

# identity 청크 유사도 임계값. 이 아래는 매칭하지 않고 미등재로 처리한다.
#
# [2026-08-23] 0.78 -> 0.70. 위 형식 보정 후 실측 분포(28개 질의):
#     정확명 8건        0.7951 ~ 0.8293
#     별칭 5건          0.5581 ~ 0.7589
#     오타 5건          0.5755 ~ 0.6993
#     실재하지만 미등재 6건 0.5639 ~ 0.6575   <- 절대 통과시키면 안 되는 구간
#     무의미 3건        0.4173 ~ 0.5406
# 0.70이 "미등재·무의미 오통과 0"을 지키는 최저선이다. 더 낮추면 '염산'이 황산으로,
# '과산화수소'가 수소로 매칭된다 — 안전 판정에서 치명적이다.
IDENTITY_MATCH_THRESHOLD = 0.70

# 1위와 2위의 점수 차 하한. 임계값만으로는 부족해서 함께 건다.
#
# 실측상 정답 케이스의 1·2위 마진은 0.10~0.26인데, 미등재·무의미는 0.006~0.026으로
# 자릿수가 다르다 — 무의미 질의는 "어느 화물과도 딱히 안 닮아서" 상위 후보들이
# 서로 붙어 있기 때문이다. 예: '메틸에틸케톤'은 0.6575로 임계 근처까지 올라오지만
# 마진이 0.0209라 이 조건에서 걸러진다.
IDENTITY_MARGIN_MIN = 0.05

# 이 값을 넘으면 결정적 매칭에 준하는 신뢰도로 본다(confidence 계산에 사용).
# [2026-08-23] 0.86 -> 0.78. 형식 보정 후에도 정확명 최고가 0.8293이라 0.86은
# 도달 불가능한 값이었다(모든 벡터 매칭이 '약한 매칭'으로 분류됐다는 뜻).
# 정확명 8건이 전부 0.7951 이상이므로 0.78이면 정확명만 '강함'으로 잡힌다.
IDENTITY_STRONG_THRESHOLD = 0.78

# "혹시 ○○을 찾으셨나요?"라고 되물을 최소 점수. 이보다 낮으면 후보를 아예 말하지
# 않는다 — 'zzzz'(0.4173)에 대고 "혹시 황산?"이라고 되묻는 건 도움이 아니라 잡음이다.
# 오타 최저(0.5755)와 무의미 최고(0.5406) 사이.
NEAR_MISS_MIN_SCORE = 0.55

# 되묻기 후보의 철자 거리 상한 비율. 점수만으로는 "오타"와 "실재하지만 이 시스템에
# 없는 물질"을 못 가른다(구간이 완전히 겹친다). 그런데 철자로는 갈린다 — 오타는
# 원래 이름에서 한 글자 어긋난 것이고, 미등재 물질은 이름 자체가 다르다. 실측:
#     오타 5건    밴젠/벤젠·톨류엔/톨루엔·가소린/가솔린·아새톤/아세톤·에타놀/에탄올 -> 전부 자모 거리 1
#     미등재 6건  염산/황산(3) 과산화수소/수소(6) 질산암모늄/암모니아(8)
#                하이드라진/수소(9) 메틸에틸케톤/메틸알코올(9) 포름알데히드/테트라하이드로푸란(14)
# 이 한 겹으로 엉뚱한 되묻기 6건이 전부 사라지고 오타 5건은 전부 남는다.
#
# 반드시 **자모 단위**로 재야 한다. 음절 단위로 재면 '에타놀'->'에탄올'이 거리 2로
# 잡혀(타/탄, 놀/올) 진짜 오타가 걸러지고, '염산'->'황산'은 거리 1로 잡혀 엉뚱한
# 되묻기가 살아남는다 — 정확히 반대로 동작한다. 한글 오타는 대개 받침 하나
# 차이인데 그건 자모 1개짜리 삽입/치환이기 때문이다.
NEAR_MISS_MAX_EDIT_RATIO = 0.25

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


_JAMO_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JAMO_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JAMO_JONG = "_ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"


def _to_jamo(text: str) -> str:
    """완성형 한글을 초·중·종성으로 풀어 쓴다. 한글이 아닌 문자는 그대로 둔다."""
    out: list[str] = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(_JAMO_CHO[code // 588])
            out.append(_JAMO_JUNG[(code % 588) // 28])
            jong = code % 28
            if jong:
                out.append(_JAMO_JONG[jong])
        else:
            out.append(ch)
    return "".join(out)


def _edit_distance(a: str, b: str) -> int:
    """레벤슈타인 거리. 이름이 짧아(대부분 2~10자) 단순 DP로 충분하다."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_plausible_typo(query: str, row: MsdsChemical) -> bool:
    """질의가 이 화물 이름의 오타로 보이는가(한글명·영문명 중 가까운 쪽 기준)."""
    q = _to_jamo(normalize_name(query))
    for name in (row.name_ko, row.name_en):
        if not name:
            continue
        target = _to_jamo(normalize_name(name))
        limit = max(1, round(min(len(q), len(target)) * NEAR_MISS_MAX_EDIT_RATIO))
        if _edit_distance(q, target) <= limit:
            return True
    return False


def _cosine_score(distance_column):
    """pgvector의 코사인 '거리'(0~2)를 유사도(1~-1)로 뒤집는다."""
    return 1 - distance_column


async def resolve_chemical_names(
    db: AsyncSession,
    embedding_client: EmbeddingClient,
    names: list[str],
) -> tuple[list[ChemicalMatch], list[str], dict[str, tuple[str, float]]]:
    """물질명 목록을 (해석 성공, 해석 실패, 되물을 후보)로 나눈다.

    같은 화물이 두 이름으로 들어오면(예: "휘발유"와 "가솔린") chem_id 기준으로
    중복 제거한다.

    세 번째 값은 해석에 실패한 이름 중 "가장 가까웠지만 임계에 못 미친" 화물이
    있는 경우의 {입력한 이름: (화물명, 점수)}다. 관제사에게 되묻기 위한 것이고
    자동 교정에는 쓰지 않는다 — 이유는 _resolve_by_vector 참고.
    """
    if not names:
        return [], [], {}

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

    near_misses: dict[str, tuple[str, float]] = {}
    if pending_vector:
        vector_matches, vector_failed, near_misses = await _resolve_by_vector(
            db, embedding_client, pending_vector
        )
        matches.extend(vector_matches)
        unresolved.extend(vector_failed)

    deduped: dict[str, ChemicalMatch] = {}
    for match in matches:
        deduped.setdefault(match.chem_id, match)
    return list(deduped.values()), unresolved, near_misses


async def _resolve_by_vector(
    db: AsyncSession,
    embedding_client: EmbeddingClient,
    names: list[str],
) -> tuple[list[ChemicalMatch], list[str], dict[str, tuple[str, float]]]:
    """(매칭 성공, 매칭 실패, 실패한 이름 -> (가장 가까웠던 화물명, 점수)).

    세 번째 값은 "혹시 이걸 찾으셨나요?" 제안용이다. **매칭에는 절대 쓰지 않는다.**
    오타(0.5755~0.6993)와 실재하지만 미등재인 물질(0.5639~0.6575)의 점수 구간이
    완전히 겹쳐서, 자동으로 교정하면 '염산'을 황산으로, '과산화수소'를 수소로
    바꿔치기하게 된다(실측). 사람에게 되묻는 것만이 안전하다.
    """
    if not await _index_has_rows(db):
        raise EmbeddingIndexEmptyError()

    vectors = await embedding_client.embed(
        [_IDENTITY_QUERY_TEMPLATE.format(n) for n in names]
    )

    matches: list[ChemicalMatch] = []
    unresolved: list[str] = []
    near_misses: dict[str, tuple[str, float]] = {}

    for raw, vector in zip(names, vectors):
        distance = MsdsEmbedding.embedding.cosine_distance(vector)
        stmt: Select = (
            select(MsdsChemical, _cosine_score(distance).label("score"))
            .join(MsdsEmbedding, MsdsEmbedding.chem_id == MsdsChemical.chem_id)
            .where(MsdsEmbedding.chunk_kind == CHUNK_KIND_IDENTITY)
            .order_by(distance)
            .limit(2)   # 마진 계산에 2위가 필요하다
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            unresolved.append(raw)
            continue

        top = rows[0]
        score = float(top.score)
        margin = score - float(rows[1].score) if len(rows) > 1 else 1.0
        name = top[0].name_ko or top[0].name_en or top[0].chem_id

        if score < IDENTITY_MATCH_THRESHOLD or margin < IDENTITY_MARGIN_MIN:
            unresolved.append(raw)
            if score >= NEAR_MISS_MIN_SCORE and _is_plausible_typo(raw, top[0]):
                near_misses[raw] = (name, round(score, 4))
            continue

        matches.append(
            _to_match(top[0], query_name=raw, method=MatchMethod.VECTOR, score=round(score, 4))
        )

    return matches, unresolved, near_misses


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
