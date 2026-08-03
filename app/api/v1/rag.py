"""MSDS 질의응답 API — `POST /api/v1/rag/query`.

기존 `POST /api/v1/chatbot/chat`을 대체한다. 로직은 새로 만들지 않고
`chatbot.service.answer_question()`을 그대로 부르는 **어댑터**다 — 같은 질문에
경로마다 다른 답이 나오는 상황을 막기 위해서다(챗봇과 대시보드가 같은 답을 내야
한다는 원칙을 API 층에서도 지킨다).

응답에서 내부 3계층(벡터 청크 / 정형 컬럼 / 그래프 관계)은 `citations`로 평탄화한다.
클라이언트가 알아야 할 것은 근거의 내용과 출처지 백엔드가 어떤 저장소를 썼는지가
아니고, 계층이 늘어도 계약이 안 바뀌어야 하기 때문이다.

다만 아래 셋은 평탄화하지 않고 별도 필드로 남긴다. 구현을 바꿔도 사라지지 않는
**안전 도메인 사실**이라, 답변 문자열에 녹이면 클라이언트가 구조적으로 처리할 수 없다.
  confidence  — 근거 종류로 코드가 산정(LLM에게 묻지 않는다)
  unresolved  — DB 미등재. "혼재금지 관계 없음"이 아니라 "판정 불가"임을 알린다
  assessment  — 규칙엔진 하한 + IMDG 공인 격리표로 보정된 위험등급
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.chatbot.citations import build_citations
from app.agents.chatbot.schemas import RagAssessment, RagQueryRequest, RagQueryResponse
from app.agents.chatbot.service import answer_question
from app.core.deps import get_embedding_client, get_llm_client, get_session
from app.core.exceptions import (
    EmbeddingGenerationError,
    EmbeddingIndexEmptyError,
    LLMGenerationError,
)
from app.llm.base import LLMClient
from app.llm.embeddings import EmbeddingClient
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/rag", tags=["rag"])

_DESCRIPTION = """
MSDS 원문·지식그래프·정형 값을 근거로 자연어 질문에 답합니다. 근거에 없는 내용은
답하지 않고 `confidence`를 `low`로 내립니다.

- **`citations[].score`가 `null`이면 확정값**(MSDS 정형 항목·지식그래프 관계)입니다.
  검색으로 근사한 게 아니라 저장된 값이라 **유사도 0.82짜리 발췌보다 신뢰도가
  높습니다.** 화면에서 유사도 뱃지 대신 "확정"으로 구분하세요.
- **`unresolved`가 비어 있지 않으면 경고를 띄우세요.** DB 미등재 물질이며,
  "혼재금지 관계가 없다(안전)"가 아니라 **"판정할 수 없다"**는 뜻입니다.
- `assessment`는 혼재 판정 질문일 때만 채워집니다. 답변 문장이 아니라 이 필드의
  `risk_level`을 결론으로 표시하세요.
- `cargo_hint`는 **화면이 이미 화물을 특정한 경우에만** 보내세요. 자유 채팅에서는
  생략합니다(사용자는 CAS번호를 모릅니다). `chem_id` 권장.
- `top_k`는 받지 않습니다 — 클라이언트가 바꾸면 같은 질문에 다른 답이 나옵니다.
  질문 유형에 따라 서버가 정합니다. **정의되지 않은 필드는 422로 거절합니다.**

배경: `05_챗봇_에이전트_설계문서.md` · `06_MSDS_지식배치_설계문서.md`
"""

_RESPONSES: dict = {
    422: {"description": "요청 형식 오류 — 빈 질문, 정의되지 않은 필드(예: `top_k`), "
                         "`chem_id`·`cas_no`가 모두 없는 `cargo_hint`"},
    502: {"description": "임베딩 또는 LLM 프로바이더 호출 실패. 재시도 가능"},
    503: {"description": "임베딩 인덱스가 비어 있음. 서버에서 `python -m scripts.embed_msds` 필요"},
}


@router.post(
    "/query",
    response_model=RagQueryResponse,
    summary="MSDS 안전 질의응답",
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
async def query(
    request: RagQueryRequest,
    db: AsyncSession = Depends(get_session),
    llm_client: LLMClient = Depends(get_llm_client),
    embedding_client: EmbeddingClient = Depends(get_embedding_client),
) -> RagQueryResponse:
    try:
        result = await answer_question(
            db, neo4j_client.driver, llm_client, embedding_client, request
        )
    except EmbeddingIndexEmptyError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except EmbeddingGenerationError as e:
        raise HTTPException(status_code=502, detail=f"임베딩 생성 실패 ({e.provider}): {e.reason}")
    except LLMGenerationError as e:
        raise HTTPException(status_code=502, detail=f"LLM 답변 생성 실패 ({e.provider}): {e.reason}")

    assessment = None
    if result.safety_assessment is not None:
        a = result.safety_assessment
        assessment = RagAssessment(
            risk_level=a.risk_level.value,
            rule_engine_floor=a.rule_engine_floor.value,
            reasoning=a.reasoning,
        )

    return RagQueryResponse(
        answer=result.answer,
        citations=build_citations(result),
        confidence=result.confidence,
        unresolved=result.chemicals_unresolved,
        assessment=assessment,
    )
