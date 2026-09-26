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
**선석 검증 화면의 주 엔드포인트.** 정해진 선석 하나에 대해 선석(가용수심·흘수 여유·이웃
화물) → 그 선석 임계값으로 기상 → 혼재 판정을 순서대로 돌려 결론 하나를 냅니다. 판정 잡
(10분 주기)과 같은 계산입니다.

**`assigned_wharf_name` 은 필수입니다.** 배가 실제로 붙은 부두(위치 판정) 또는 접안 전이면
PORT-MIS 신고 선석을 넣으세요. [2026-09-27] 선석을 새로 고르는 탐색모드를 걷어냈습니다 —
배정은 항만공사 선석회의가 하고, 우리는 그 배정이 조건에 맞는지 확인합니다. 다른 자리가
필요하면 `POST /scheduling/alternatives`(대체 선석 제안)를 쓰세요.

**`overall_decision`** 은 아직 배정 주체의 어휘(`승인가능`/`적합선석없음` …)로 나옵니다.
화면에 그대로 쓰지 마세요 — `assessment_history.level`(적합/주의/부적합/판정불가)로
옮겨진 값을 쓰거나, `/orchestrator/assess-and-record` 를 부르면 그 변환까지 끝납니다.

`assignment_trace`는 판단 경로를 사람이 읽는 문장으로 담습니다.

배경: `04_오케스트레이터_설계문서.md`
"""

_RESPONSES: dict = {
    404: {"description": "요청한 화물의 MSDS를 찾을 수 없음 (KOSHA 미등재 CAS/chem_id)"},
    422: {"description": "요청 형식 오류 — assigned_wharf_name 누락 포함"},
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
# [2026-09-27] facility_name 은 PORT-MIS(10분 주기) 신고 계류시설이다. 예전엔
# dashboard_current.facility_name(= 하루 1번 수집되는 VTS 이력의 첫 행)을 읽어
# 스냅샷의 portmis_facility 칸에 VTS 선석이 남았다(arrival_watcher 와 같은 문제).
_QUERY_LIVE_STATE = text("""
    SELECT dc.nav_status_code, dc.received_at_utc, pm.arrival_facility_nm AS facility_name, dc.draught
    FROM mart.dashboard_current dc
    LEFT JOIN LATERAL (
        SELECT p.arrival_facility_nm
        FROM portmis_vessel p
        WHERE upper(btrim(p.callsgn)) = upper(btrim(dc.callsgn))
          AND p.arrival_at_utc <= now() + interval '12 hours'
        ORDER BY p.arrival_at_utc DESC
        LIMIT 1
    ) pm ON true
    WHERE dc.callsgn = :call_sign
    ORDER BY dc.received_at_utc DESC NULLS LAST
    LIMIT 1
""")


class AssessAndRecordResult(BaseModel):
    result: OrchestratorResult
    recorded: bool = Field(
        description="판정이 실제로 기록됐는가. 직전 판정과 시점·등급·조치안이 모두 같으면 "
        "기록하지 않는다(변화만 남긴다) — 그때 False"
    )
    level: str = Field(description="적합 | 주의 | 확인요청 | 부적합 | 판정불가")
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
