"""챗봇 보조 조회 API.

질의응답 자체는 `POST /api/v1/rag/query`(app/api/v1/rag.py)로 옮겼다. 여기 남은 두
엔드포인트는 화면 구성용 목록 조회라 RAG 파이프라인과 성격이 다르다.
"""

from fastapi import APIRouter, HTTPException

from app.agents.chatbot import graph_queries
from app.agents.chatbot.schemas import ChemicalListItem, IncompatibleCategoryGroup
from app.agents.chatbot.service import build_incompatible_groups
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/chatbot", tags=["chatbot"])


@router.get("/chemicals", response_model=list[ChemicalListItem], summary="챗봇 추론 가능 화물 목록 조회")
async def list_chemicals() -> list[ChemicalListItem]:
    """챗봇이 실제로 추론할 수 있는 화물 목록.

    출처를 PostgreSQL(msds_chemical)이 아니라 Neo4j로 잡은 건 의도적이다 — MSDS
    원문만 있고 그래프에 없는 화물은 혼재금지 질문에 답할 수 없어서, 사용자에게
    "물어보면 답이 나오는 목록"으로 보여줘야 할 것은 그래프 등재분이다.
    """
    rows = await graph_queries.fetch_all_chemicals(neo4j_client.driver)
    return [ChemicalListItem(**row) for row in rows]


@router.get(
    "/chemicals/{chem_id}/incompatibles",
    response_model=list[IncompatibleCategoryGroup],
    summary="화물별 혼재금지 카테고리 조회",
)
async def list_incompatibles(chem_id: str) -> list[IncompatibleCategoryGroup]:
    """특정 화물의 혼재금지 카테고리와 각 카테고리에 속하는 등재 화물."""
    profiles = await graph_queries.fetch_profiles(neo4j_client.driver, [chem_id])
    if chem_id not in profiles:
        raise HTTPException(
            status_code=404, detail=f"지식그래프에 등재되지 않은 chem_id 입니다: {chem_id}"
        )
    return await build_incompatible_groups(neo4j_client.driver, chem_id)
