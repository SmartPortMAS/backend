"""종합 판정 API.

[2026-09-21 전면 개편] 배정을 만드는 두 엔드포인트를 없앴다.

없앤 것:
  POST /orchestrator/assess-and-commit → BerthAssignment(status=APPROVED) INSERT
  POST /orchestrator/reject            → BerthAssignment(status=REJECTED) INSERT

둘 다 관제사 콘솔에서 누른 버튼이 **자리를 잠그던** 경로다. 우리는 자리를
잠그지 않는다(9/17 회의 §1, 방향 C). 대신 같은 자리에 판정을 남기는
`POST /orchestrator/assess-and-record` 를 뒀다 — 콘솔에서 배 하나를 골라
지금 바로 판정하고 그 결과를 `assessment_history` 에 기록한다.

`/assess` 는 그대로다. 판단만 하고 아무것도 쓰지 않는다.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.schemas import OrchestratorRequest, OrchestratorResult
from app.agents.orchestrator.service import orchestrate
from app.core.deps import get_llm_client, get_session
from app.core.exceptions import (
    CargoCategoryUnknownError,
    LLMGenerationError,
    MsdsNotFoundError,
    MsdsUpstreamError,
)
from app.llm.base import LLMClient
from app.neo4j_client import neo4j_client
from app.services.assessment import (
    level_from_decision,
    record_from_orchestrator,
    stage_from_nav_status,
)

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])

_DESCRIPTION = """
**선석 검증 화면의 주 엔드포인트.** 기상 → 선석 후보 → 선석별 기상 재판정 → 혼재 판정을
순서대로 돌려 결론 하나를 냅니다. 개별 에이전트 API를 따로 호출해 합치지 마세요 —
기상이 불가면 선석 탐색을 건너뛰고, 선석 확정 후 그 부두그룹 임계값으로 기상을 다시
판정하는 순서가 여기 들어 있습니다.

**`assigned_wharf_name` 을 반드시 채워 주세요(검증모드).** 비우면 118석 전체를
탐색해 top-3를 고릅니다 — 이 시스템이 하지 않기로 한 동작입니다. 배정은 항만공사
선석회의가 하고, 우리는 그 배정이 조건에 맞는지 확인합니다.

**`overall_decision`** 은 아직 배정 주체의 어휘(`승인가능`/`적합선석없음` …)로 나옵니다.
화면에 그대로 쓰지 마세요 — `assessment_history.level`(적합/주의/부적합/판정불가)로
옮겨진 값을 쓰거나, `/orchestrator/assess-and-record` 를 부르면 그 변환까지 끝납니다.

`assignment_trace`는 판단 경로를 사람이 읽는 문장으로 담습니다.

배경: `04_오케스트레이터_설계문서.md`
"""

_RESPONSES: dict = {
    404: {"description": "요청한 화물의 MSDS를 찾을 수 없음 (KOSHA 미등재 CAS/chem_id)"},
    422: {"description": "요청 형식 오류, 또는 화물에 선석 카테고리(cargo_category)가 "
                         "지정되지 않아 후보 탐색 불가"},
    502: {"description": "KOSHA MSDS API 또는 LLM 호출 실패. 재시도 가능"},
}


async def _orchestrate_or_http(
    db: AsyncSession, llm_client: LLMClient, request: OrchestratorRequest,
) -> OrchestratorResult:
    try:
        return await orchestrate(db, neo4j_client.driver, llm_client, request)
    except MsdsNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"MSDS not found for identifier: {e.identifier}")
    except MsdsUpstreamError as e:
        raise HTTPException(status_code=502, detail=f"KOSHA MSDS API 호출 실패: {e.reason}")
    except CargoCategoryUnknownError as e:
        raise HTTPException(
            status_code=422,
            detail=f"화물({e.chem_id})에 선석 카테고리(cargo_category)가 지정되어 있지 않습니다.",
        )
    except LLMGenerationError as e:
        raise HTTPException(status_code=502, detail=f"LLM 판단 생성 실패 ({e.provider}): {e.reason}")


@router.post(
    "/assess",
    response_model=OrchestratorResult,
    summary="종합 판정 (기상 + 스케줄링 + 안전관제) — 아무것도 기록하지 않음",
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
async def assess(
    request: OrchestratorRequest,
    db: AsyncSession = Depends(get_session),
    llm_client: LLMClient = Depends(get_llm_client),
) -> OrchestratorResult:
    return await _orchestrate_or_http(db, llm_client, request)


# ---------------------------------------------------------------------------
# 판정 기록 (2026-09-21 — 옛 assess-and-commit 자리)
#
# 관제사가 콘솔에서 배 하나를 골라 "지금 판정"을 누르는 경우다. 배경 잡
# (watch_arrivals, 10분)이 아직 그 배를 안 훑었을 수 있으므로 즉석으로 돌린다.
#
# 옛 판은 여기서 berth_assignment 를 status=APPROVED 로 INSERT 했다 — 콘솔
# 버튼이 곧 배정이었다. 지금은 판정 1건을 남길 뿐이고, 어떤 자원도 잠기지 않는다.
# ---------------------------------------------------------------------------


class AssessAndRecordRequest(OrchestratorRequest):
    call_sign: str = Field(description="선박 호출부호 — 판정 행의 식별 키")
    vessel_name: str | None = None


# 시점(stage)은 AIS 항해상태로만 정한다 — 프런트가 보낸 값을 믿지 않고 여기서 읽는다.
# PORT-MIS 를 쓰지 않는 이유는 app/models/assessment_history.py::AssessmentStage 참고.
_QUERY_LIVE_STATE = text("""
    SELECT nav_status_code, received_at_utc, facility_name, draught
    FROM mart.dashboard_current
    WHERE callsgn = :call_sign
    ORDER BY received_at_utc DESC NULLS LAST
    LIMIT 1
""")


class AssessAndRecordResult(BaseModel):
    result: OrchestratorResult
    recorded: bool = Field(
        description="판정이 실제로 기록됐는가. 직전 판정과 시점·등급·조치안이 모두 같으면 "
        "기록하지 않는다(변화만 남긴다) — 그때 False"
    )
    level: str = Field(description="적합 | 주의 | 부적합 | 판정불가")
    stage: str | None = Field(default=None, description="AIS 항해상태로 정한 시점. 상태를 모르면 None")


@router.post(
    "/assess-and-record",
    response_model=AssessAndRecordResult,
    summary="종합 판정 + 판정 이력 기록 (배정하지 않음)",
    responses=_RESPONSES,
)
async def assess_and_record(
    request: AssessAndRecordRequest,
    db: AsyncSession = Depends(get_session),
    llm_client: LLMClient = Depends(get_llm_client),
) -> AssessAndRecordResult:
    if not request.assigned_wharf_name:
        raise HTTPException(
            status_code=422,
            detail=(
                "assigned_wharf_name 이 필요합니다 — 이 시스템은 선석을 고르지 않고 "
                "이미 배정된 시설이 조건에 맞는지 확인합니다."
            ),
        )

    base = OrchestratorRequest(**request.model_dump(include=set(OrchestratorRequest.model_fields)))
    result = await _orchestrate_or_http(db, llm_client, base)

    live = (await db.execute(_QUERY_LIVE_STATE, {"call_sign": request.call_sign})).mappings().first()
    stage = stage_from_nav_status(live["nav_status_code"] if live else None)

    recorded = await record_from_orchestrator(
        db,
        call_sign=request.call_sign,
        vessel_name=request.vessel_name,
        stage=stage,
        wharf_name=request.assigned_wharf_name,
        result=result,
        input_snapshot={
            "source": "관제사 콘솔",
            "target_facility": request.assigned_wharf_name,
            "portmis_facility": live["facility_name"] if live else None,
            "nav_status_code": live["nav_status_code"] if live else None,
            "position_at_utc": (
                live["received_at_utc"].isoformat()
                if live and isinstance(live["received_at_utc"], datetime) else None
            ),
            "draught_m": float(request.vessel.draught_m) if request.vessel.draught_m else None,
            "requested_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )
    await db.commit()

    level, _ = level_from_decision(result)
    return AssessAndRecordResult(
        result=result, recorded=recorded, level=level.value,
        stage=stage.value if stage is not None else None,
    )
