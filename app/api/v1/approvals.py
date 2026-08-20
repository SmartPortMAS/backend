"""관제사 사전승인 API (08_스케줄링_전면재설계_자동배정_설계문서.md §5.3).

07 문서 §5.3·§6이 설계했던 승인 API를 재사용하되, 입력이 다르다 — 07 문서는
"이미 정해진 사전배정을 검증한 결과"를 승인 대상으로 삼았지만, 이번 설계는
"시스템(arrival_watcher/anchorage_promoter)이 자체 계산해 이미 INSERT까지 해 둔
추천(status=REQUESTED)"을 승인 대상으로 삼는다. 그래서 승인/반려가 새 배정을
만드는 게 아니라 이미 있는 REQUESTED 행의 status만 바꾸는, 더 단순한 구조다.

GET /approvals/pending은 berth_assignment(REQUESTED)와 anchorage_queue(WAITING)를
합쳐서 보여준다 — 후자는 승인/반려 버튼이 필요 없는 정보성 항목이다(§5.3, 정박지는
잠금 대상이 아니므로).
"""

from datetime import datetime, timezone
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.models.anchorage_queue import STATUS_WAITING
from app.models.berth_assignment import STATUS_APPROVED, STATUS_REJECTED, STATUS_REQUESTED

router = APIRouter(prefix="/approvals", tags=["approvals"])


class PendingKind(str, Enum):
    BERTH_ASSIGNMENT = "선석배정"
    ANCHORAGE_WAIT = "정박지대기"


class PendingItem(BaseModel):
    kind: PendingKind
    id: int
    call_sign: str | None
    vessel_name: str | None
    cargo_chem_id: str | None
    berth_id: str | None = Field(default=None, description="kind=선석배정일 때만 값 있음")
    slot_no: int | None = None
    anchorage_id: str | None = Field(default=None, description="kind=정박지대기일 때만 값 있음")
    assignment_reason: str | None
    created_at: datetime


_QUERY_PENDING_ASSIGNMENTS = text("""
    SELECT id, call_sign, vessel_name, cargo_chem_id, berth_id, slot_no,
           assignment_reason, created_at
    FROM berth_assignment
    WHERE status = :status
    ORDER BY created_at ASC
""")

_QUERY_PENDING_ANCHORAGE = text("""
    SELECT id, call_sign, vessel_name, cargo_chem_id, anchorage_id,
           assignment_reason, created_at
    FROM anchorage_queue
    WHERE status = :status
    ORDER BY entered_at ASC
""")


@router.get(
    "/pending",
    response_model=list[PendingItem],
    summary="승인 대기 목록 (선석배정 추천 + 정박지 대기, §5.3)",
)
async def get_pending(db: AsyncSession = Depends(get_session)) -> list[PendingItem]:
    assignments = (
        await db.execute(_QUERY_PENDING_ASSIGNMENTS, {"status": STATUS_REQUESTED})
    ).mappings().all()
    anchorage = (
        await db.execute(_QUERY_PENDING_ANCHORAGE, {"status": STATUS_WAITING})
    ).mappings().all()

    out = [
        PendingItem(
            kind=PendingKind.BERTH_ASSIGNMENT, id=r["id"], call_sign=r["call_sign"],
            vessel_name=r["vessel_name"], cargo_chem_id=r["cargo_chem_id"],
            berth_id=r["berth_id"], slot_no=r["slot_no"],
            assignment_reason=r["assignment_reason"], created_at=r["created_at"],
        )
        for r in assignments
    ] + [
        PendingItem(
            kind=PendingKind.ANCHORAGE_WAIT, id=r["id"], call_sign=r["call_sign"],
            vessel_name=r["vessel_name"], cargo_chem_id=r["cargo_chem_id"],
            anchorage_id=r["anchorage_id"],
            assignment_reason=r["assignment_reason"], created_at=r["created_at"],
        )
        for r in anchorage
    ]
    out.sort(key=lambda item: item.created_at)
    return out


class Verdict(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class DecisionRequest(BaseModel):
    verdict: Verdict
    approved_by: str = Field(description="승인/반려한 관제사 식별자(자유 텍스트 — 인증 체계 없음, 07 문서 §8.6 한계 그대로)")
    reason: str | None = Field(default=None, description="반려 사유 등 추가 코멘트")


class DecisionResult(BaseModel):
    id: int
    status: str
    approved_by: str


_QUERY_GET_ASSIGNMENT_STATUS = text("SELECT status FROM berth_assignment WHERE id = :id")

_QUERY_APPLY_DECISION = text("""
    UPDATE berth_assignment
    SET status = :status, approved_by = :approved_by, updated_at = :now,
        assignment_reason = COALESCE(:reason, assignment_reason)
    WHERE id = :id AND status = :expected_status
""")


@router.post(
    "/{assignment_id}/decision",
    response_model=DecisionResult,
    summary="선석배정 추천 승인/반려 (§5.3 — 실제 배정이 확정되는 유일한 지점)",
)
async def decide(
    assignment_id: int, body: DecisionRequest, db: AsyncSession = Depends(get_session),
) -> DecisionResult:
    row = (await db.execute(_QUERY_GET_ASSIGNMENT_STATUS, {"id": assignment_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"berth_assignment id={assignment_id}를 찾을 수 없습니다.")
    current_status = row[0]
    if current_status != STATUS_REQUESTED:
        raise HTTPException(
            status_code=409,
            detail=f"이미 처리된 건입니다(현재 status={current_status}) — REQUESTED 상태만 승인/반려할 수 있습니다.",
        )

    new_status = STATUS_APPROVED if body.verdict is Verdict.APPROVE else STATUS_REJECTED
    result = await db.execute(
        _QUERY_APPLY_DECISION,
        {
            "id": assignment_id, "status": new_status, "approved_by": body.approved_by,
            "now": datetime.now(timezone.utc), "reason": body.reason,
            "expected_status": STATUS_REQUESTED,
        },
    )
    if result.rowcount == 0:
        # 조회와 갱신 사이의 경합(동시에 다른 관제사가 먼저 처리) — 409로 알린다.
        await db.rollback()
        raise HTTPException(status_code=409, detail="처리 중 다른 요청이 먼저 반영됐습니다. 목록을 새로고침하세요.")
    await db.commit()

    return DecisionResult(id=assignment_id, status=new_status, approved_by=body.approved_by)
