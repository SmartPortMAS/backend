from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import SafetyAssessmentRequest, SafetyAssessmentResult
from app.agents.safety.service import assess_safety
from app.core.deps import get_llm_client, get_session
from app.core.exceptions import LLMGenerationError, MsdsNotFoundError, MsdsUpstreamError
from app.llm.base import LLMClient
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/safety", tags=["safety"])

_DESCRIPTION = """
대상 화물과 인접 선석 화물들의 **혼재 위험**을 판정합니다. MSDS 텍스트 기반 혼재금지와
IMDG Code 공인 격리표를 함께 보고 4단계 등급을 냅니다.

> 선석 배정 화면이라면 이 API 대신 **`POST /orchestrator/assess`**를 쓰세요. 기상·선석
> 후보 탐색까지 묶어 최종 결론 하나를 냅니다. 이 API는 "이미 화물 조합이 정해진 상태"의
> 단독 판정용입니다.

**기상은 다루지 않습니다.** 기상 임계값 판정은 기상분석 에이전트(`/weather/assess`)의
책임이고, 기상×안전 조합은 오케스트레이터가 처리합니다.

### `risk_level` 읽는 법

`안전` < `주의` < `위험` < `배정불가` 순으로 심각합니다. **`배정불가`면 접안을 막아야
합니다.**

### `rule_engine_floor`가 핵심입니다

`risk_level`은 LLM이 정하지만, `rule_engine_floor`는 **그래프 탐색으로 계산한 결정적
하한**입니다(MSDS 혼재금지와 IMDG 격리표 중 더 심각한 쪽). LLM은 이 하한보다 낮출 수
없습니다.

두 값이 다르면 LLM이 하한보다 위험하다고 본 것입니다. 화면에 둘 다 노출하면 관제사가
"규칙이 정한 최소 등급"과 "AI가 추가로 본 위험"을 구분할 수 있습니다.

### 충돌 근거 두 종류

| 필드 | 출처 | 성격 |
|---|---|---|
| `conflicts` | MSDS 원문에서 추출한 혼재금지 키워드 | 텍스트 기반, 참고 |
| `imdg_conflicts` | IMDG Code Chapter 7.2 공인 일반 격리표 | **법적 근거**, 우선 표시 권장 |

`imdg_conflicts[].segregation_code`는 IMDG 격리코드(1~4)로 숫자가 클수록 강한 격리를
요구합니다.

`msds_sections_used`는 판정 근거로 쓴 MSDS 섹션 키 목록입니다. 답변 검증용으로
노출하세요.
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
