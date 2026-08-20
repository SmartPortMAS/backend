from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.schemas import OrchestratorRequest, OrchestratorResult, OverallDecision
from app.agents.orchestrator.service import orchestrate
from app.agents.scheduling.occupancy import find_free_slot
from app.core.deps import get_llm_client, get_session
from app.core.exceptions import (
    CargoCategoryUnknownError,
    LLMGenerationError,
    MsdsNotFoundError,
    MsdsUpstreamError,
)
from app.llm.base import LLMClient
from app.models.berth_assignment import STATUS_APPROVED, STATUS_REJECTED, BerthAssignment
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])

_DESCRIPTION = """
**선석 배정 화면의 주 엔드포인트.** 기상 → 선석 후보 → 선석별 기상 재판정 → 혼재 판정을
순서대로 돌려 결론 하나를 냅니다. 개별 에이전트 API를 따로 호출해 합치지 마세요 —
기상이 불가면 선석 탐색을 건너뛰고, 선석 확정 후 그 부두그룹 임계값으로 기상을 다시
판정하는 순서가 여기 들어 있습니다.

**`overall_decision`**

| 값 | 함께 볼 필드 |
|---|---|
| `승인가능` | `selected_berth` |
| `기상불가_중단권고` | `weather_assessment.reasons` |
| `적합선석없음` | — |
| `전후보배정불가` | `rejected_candidates` |
| `정박지대기` | `anchorage_assignment` |

**`승인가능` 외 네 상태는 모두 "지금 하역하면 안 되는" 상태**입니다. 관제사가 원인을
구분하도록 분리한 것이니 화면에서도 구분해 주세요.

`assignment_trace`는 전용 → 대체 → 정박지 판단 경로를 사람이 읽는 문장으로 담습니다.
왜 이 선석이 나왔는지 설명할 때 그대로 노출하면 됩니다.

배경: `04_오케스트레이터_설계문서.md`
"""

_RESPONSES: dict = {
    404: {"description": "요청한 화물의 MSDS를 찾을 수 없음 (KOSHA 미등재 CAS/chem_id)"},
    422: {"description": "요청 형식 오류, 또는 화물에 선석 카테고리(cargo_category)가 "
                         "지정되지 않아 후보 탐색 불가"},
    502: {"description": "KOSHA MSDS API 또는 LLM 호출 실패. 재시도 가능"},
}


@router.post(
    "/assess",
    response_model=OrchestratorResult,
    summary="선석 배정 종합 판정 (기상 + 스케줄링 + 안전관제)",
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
async def assess(
    request: OrchestratorRequest,
    db: AsyncSession = Depends(get_session),
    llm_client: LLMClient = Depends(get_llm_client),
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


# ---------------------------------------------------------------------------
# 즉석 확정 (2026-08-19 신설)
#
# /assess는 판단만 하고 아무것도 쓰지 않는다 — 실제 배정은 arrival_watcher(10분
# 주기 배경 잡)가 이미 만들어 둔 REQUESTED 행을 POST /approvals/{id}/decision으로
# 승인해야만 생긴다. 그런데 관제사가 종합에이전트 콘솔에서 임의의 배를 직접 골라
# /assess를 부르면, 그 배가 아직 arrival_watcher 주기에 안 걸렸을 수 있다 — 이때
# "승인"을 눌러도 승인할 REQUESTED 행 자체가 없어 아무 일도 안 일어난다(실사용
# 중 발견: 관제사가 승인을 눌렀는데 선석배정현황에 계속 대기로 뜸).
#
# 이 엔드포인트는 그 경우를 위한 것이다 — 판단과 확정을 한 번에 한다: 다시
# orchestrate()를 돌려(콘솔이 보여준 결과와 승인 시점 사이에 다른 배가 그 자리를
# 먼저 가져갔을 수 있으므로 그대로 믿지 않고 재검증한다) APPROVED가 나오면 그
# 자리에서 slot_no를 잡아 status=APPROVED로 바로 INSERT한다. REQUESTED를 거치지
# 않는다 — 관제사가 이미 이 자리에서 승인을 결정했으므로 별도 승인 대기가 의미가
# 없다.
# ---------------------------------------------------------------------------


class AssessAndCommitRequest(OrchestratorRequest):
    call_sign: str = Field(description="선박 호출부호 — 배정 행 식별 키")
    vessel_name: str | None = None
    imo_no: str | None = None
    approved_by: str = Field(description="이 확정을 실행한 관제사")


# arrival_watcher._QUERY_PENDING_ARRIVALS와 동일 소스(mart.dashboard_current —
# upa_port_call/portmis_vessel을 이미 조인해 둔 뷰)에서 VTS 확인 입출항 시각을
# 가져온다. 프론트가 보낸 window_start/window_end는 계획값(관제사 콘솔에서는
# "지금~+8시간" 고정값)이라 실제 입출항 시각과 다르다 — actual_berthing_at/
# actual_departure_at은 반드시 이 확정 관측치로 채워야 한다(모델 주석: "계획이
# 아니라 사후 확인값").
_QUERY_ACTUAL_PORT_CALL_TIMES = text("""
    SELECT arrival_at_utc, departure_at_utc
    FROM mart.dashboard_current
    WHERE callsgn = :call_sign
    ORDER BY received_at_utc DESC NULLS LAST
    LIMIT 1
""")


class AssessAndCommitResult(BaseModel):
    result: OrchestratorResult
    committed: bool = Field(description="실제로 berth_assignment가 생성됐는가")
    assignment_id: int | None = Field(default=None, description="committed=True일 때 생성된 행의 id")
    not_committed_reason: str | None = Field(
        default=None, description="committed=False인 이유(사람이 읽는 문장) — result만으로는 "
        "'왜 못 만들었는지'(만석 vs 배정불가)가 구분 안 되므로 별도로 둔다"
    )


@router.post(
    "/assess-and-commit",
    response_model=AssessAndCommitResult,
    summary="종합 판정 + 즉석 확정 (관제사가 콘솔에서 직접 승인, §5.3)",
    responses=_RESPONSES,
)
async def assess_and_commit(
    request: AssessAndCommitRequest,
    db: AsyncSession = Depends(get_session),
    llm_client: LLMClient = Depends(get_llm_client),
) -> AssessAndCommitResult:
    try:
        result = await orchestrate(db, neo4j_client.driver, llm_client, request)
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

    if result.overall_decision is not OverallDecision.APPROVED or not result.selected_berth:
        return AssessAndCommitResult(
            result=result, committed=False,
            not_committed_reason=f"판정 결과가 {result.overall_decision.value}라 확정할 선석이 없습니다.",
        )

    slot_no = await find_free_slot(
        db, berth_id=result.selected_berth.berth_id,
        window_start=request.window_start, window_end=request.window_end,
    )
    if slot_no is None:
        return AssessAndCommitResult(
            result=result, committed=False,
            not_committed_reason=f"추천 선석 '{result.selected_berth.wharf_name}'이 방금 만석이 "
            "됐습니다(판정과 확정 사이의 경합) — 다시 실행해 보세요.",
        )

    actual_times = (
        await db.execute(_QUERY_ACTUAL_PORT_CALL_TIMES, {"call_sign": request.call_sign})
    ).mappings().first()

    now = datetime.now(timezone.utc)
    assignment = BerthAssignment(
        berth_id=result.selected_berth.berth_id,
        slot_no=slot_no,
        call_sign=request.call_sign,
        imo_no=request.imo_no,
        vessel_name=request.vessel_name,
        cargo_chem_id=request.cargo.chem_id,
        planned_window=(request.window_start, request.window_end),
        # VTS 확인 입출항 시각(mart.dashboard_current) — 아직 출항 전이면
        # departure_at_utc는 null 그대로 둔다(추측 금지, arrival_watcher와 동일 원칙).
        actual_berthing_at=actual_times["arrival_at_utc"] if actual_times else None,
        actual_departure_at=actual_times["departure_at_utc"] if actual_times else None,
        status=STATUS_APPROVED,
        approved_by=request.approved_by,
        assignment_reason=result.summary,
        rejected_candidates=result.decision_detail(),
        created_at=now,
        updated_at=now,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)

    return AssessAndCommitResult(result=result, committed=True, assignment_id=assignment.id)


# ---------------------------------------------------------------------------
# 즉석 반려 (2026-08-20 신설)
#
# assess-and-commit의 반려판, arrival_watcher가 만든 REQUESTED 행이 없는 배도
# 콘솔에서 바로 반려할 수 있어야 한다(관제사가 종합 판정을 보고 승인/반려를
# 그 자리에서 정한다는 게 이 콘솔의 원래 목적 — REQUESTED 행의 유무는 그
# 판단과 무관해야 한다). berth_id/slot_no/planned_window 없이 REJECTED 행만
# 남긴다 — 애초에 자원을 점유한 적 없는 반려이므로 EXCLUDE 제약 대상도 아니다
# (berth_assignment.py 모델 주석 참고). arrival_watcher의 NOT EXISTS 조건은
# REJECTED를 제외 대상에서 빼므로, 이 배는 다음 주기에 다시 후보로 잡힌다 —
# 반려가 영구 배제는 아니다.
# ---------------------------------------------------------------------------


class RejectRequest(BaseModel):
    call_sign: str = Field(description="선박 호출부호 — 배정 행 식별 키")
    vessel_name: str | None = None
    imo_no: str | None = None
    cargo_chem_id: str | None = None
    rejected_by: str = Field(description="이 반려를 실행한 관제사")
    reason: str | None = Field(default=None, description="반려 사유(콘솔이 보여준 판정 요약 등)")


class RejectResult(BaseModel):
    assignment_id: int


@router.post(
    "/reject",
    response_model=RejectResult,
    summary="종합 판정 반려 (관제사가 콘솔에서 직접 반려, §5.3)",
)
async def reject(request: RejectRequest, db: AsyncSession = Depends(get_session)) -> RejectResult:
    now = datetime.now(timezone.utc)
    assignment = BerthAssignment(
        call_sign=request.call_sign,
        imo_no=request.imo_no,
        vessel_name=request.vessel_name,
        cargo_chem_id=request.cargo_chem_id,
        status=STATUS_REJECTED,
        approved_by=request.rejected_by,
        assignment_reason=request.reason,
        created_at=now,
        updated_at=now,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)

    return RejectResult(assignment_id=assignment.id)
