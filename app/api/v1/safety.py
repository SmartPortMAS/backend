from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import (
    SafetyAssessmentRequest,
    SafetyAssessmentResult,
    SafetyVerdict,
)
from app.agents.safety.service import assess_safety, assess_verdict
from app.core.deps import get_llm_client, get_session
from app.core.exceptions import LLMGenerationError, MsdsNotFoundError, MsdsUpstreamError
from app.llm.base import LLMClient
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/safety", tags=["safety"])

_DESCRIPTION = """
**서로 다른 부두에 접안한 선박 사이**의 혼재 위험을 판정합니다. 판정 근거는 MSDS 텍스트
기반 혼재금지, 벌크 액체화학물질 호환성그룹, 용기등급 대비 하역방식 세 축입니다.

> 선석 배정 화면이라면 `POST /orchestrator/assess`를 쓰세요. 이 API는 화물 조합이 이미
> 정해진 상태의 단독 판정용입니다. 기상은 다루지 않습니다.

- **`risk_level`**: `안전` < `주의` < `위험` < `배정불가`. **`배정불가`면 접안을 막으세요.**
- **`rule_engine_floor`**는 그래프 탐색으로 계산한 결정적 하한이고 LLM은 이보다 낮출 수
  없습니다. `risk_level`과 함께 노출하면 "규칙이 정한 최소 등급"과 "AI가 추가로 본
  위험"을 구분할 수 있습니다.
- **`imdg_conflicts`·`imdg_unconfirmed_pairs`·`imdg_classes`는 참고 정보이며 판정 근거가
  아닙니다** (2026-08-23 변경). IMDG Code Ch.7.2는 단일 선박 내 적부 규정(이격거리
  3~24m)이라 부두와 부두 사이에는 적용 대상이 없고, IMO도 항만 구역은 별도 문서
  (MSC.1/Circ.1216)로 분리해 두었습니다. 화면에 표시하더라도 **"이 배치가 규정 위반"인
  것처럼 쓰지 마세요.** `distance_m` 역시 같은 이유로 표시용입니다.
  IMDG를 위험 판정에 쓰는 곳은 동일 선석 동시 취급을 보는 `GET /dashboard/berth-alerts`
  입니다.
- `packaging_violations`는 인접 화물과 무관하게 대상 화물 자신의 용기등급 대비
  하역방식만으로 판정합니다. `target_cargo.unload_method_name`을 안 보내면 이
  목록은 항상 비어 있습니다(판정 안 함이지 "적합"이 아님).
- `msds_sections_used`는 판정 근거로 쓴 MSDS 섹션 키입니다.

배경: `01_안전관제_에이전트_설계문서.md`
"""

_RESPONSES: dict = {
    404: {"description": "대상 또는 인접 화물의 MSDS를 찾을 수 없음 (KOSHA 미등재)"},
    502: {"description": "KOSHA MSDS API 또는 LLM 호출 실패. 재시도 가능"},
}


@router.post(
    "/assess",
    response_model=SafetyAssessmentResult,
    summary="화물 혼재 위험 판정",
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
async def assess(
    request: SafetyAssessmentRequest,
    db: AsyncSession = Depends(get_session),
    llm_client: LLMClient = Depends(get_llm_client),
) -> SafetyAssessmentResult:
    try:
        return await assess_safety(db, neo4j_client.driver, llm_client, request)
    except MsdsNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"MSDS not found for identifier: {e.identifier}")
    except MsdsUpstreamError as e:
        raise HTTPException(status_code=502, detail=f"KOSHA MSDS API 호출 실패: {e.reason}")
    except LLMGenerationError as e:
        raise HTTPException(status_code=502, detail=f"LLM 판단 생성 실패 ({e.provider}): {e.reason}")


_VERDICT_DESCRIPTION = """
**LLM 없이 등급과 근거만** 즉시 반환합니다 (실측 약 45ms). 화면이 결론을 먼저 띄우고
`POST /safety/assess`의 서술(체크리스트·근거문장)을 이어서 채우는 2단계 렌더링용입니다.

- **여기서 받은 `risk_level`은 `/safety/assess`가 주는 값과 항상 같습니다.** 등급은
  규칙엔진이 확정하므로 나중에 뒤집히지 않습니다 — 먼저 표시해도 안전합니다.
- 응답에는 `checklist`·`key_hazards`·`reasoning`이 없습니다. 그 셋만 LLM이 만듭니다.
- 판정 로직은 `/safety/assess`와 같은 함수를 씁니다. 두 엔드포인트가 같은 입력에
  다른 등급을 낼 수 없습니다.
"""


@router.post(
    "/verdict",
    response_model=SafetyVerdict,
    summary="혼재 위험 등급만 즉시 판정 (LLM 미사용)",
    description=_VERDICT_DESCRIPTION,
    responses={
        404: {"description": "대상 또는 인접 화물의 MSDS를 찾을 수 없음 (KOSHA 미등재)"},
        502: {"description": "KOSHA MSDS API 호출 실패. 재시도 가능"},
    },
)
async def verdict(
    request: SafetyAssessmentRequest,
    db: AsyncSession = Depends(get_session),
) -> SafetyVerdict:
    try:
        return await assess_verdict(db, neo4j_client.driver, request)
    except MsdsNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"MSDS not found for identifier: {e.identifier}")
    except MsdsUpstreamError as e:
        raise HTTPException(status_code=502, detail=f"KOSHA MSDS API 호출 실패: {e.reason}")
