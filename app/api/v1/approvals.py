"""관제사 **확인·조치 기록** API.

[2026-09-21 전면 개편] 승인이 아니다.

9/17 회의 "오늘 정할 것 ①" — 동안님의 '승인/반려 제거' 제안에 대한 결정은
**"버튼은 유지하고 의미를 '확인·조치 기록'으로 바꾼다"** 였다. 이 라우터가 그
결정을 코드로 옮긴 것이다.

무엇이 달라졌나:

    이전:  POST /approvals/{id}/decision  → berth_assignment.status = 'APPROVED'
           주석이 스스로 "실제 배정이 확정되는 유일한 지점"이라 밝히고 있었다.

    지금:  POST /approvals/{id}/acknowledge → assessment_history.acknowledged_by
           **아무것도 확정되지 않는다.** 관제사가 이 판정을 봤다는 사실만 남는다.

현우님이 B3(화면 문구 변경)를 보류하며 남긴 판단 — *"지금 버튼은 실제로 배정을
확정하므로 라벨만 바꾸면 화면이 거짓말"* — 이 정확했다. 그래서 라벨이 아니라
동작을 바꿨다. 이제 화면이 "확인"이라고 적으면 실제로 확인만 한다.

승인/반려의 이분법도 없앴다. 우리가 낸 것은 요청이 아니라 의견이므로 반려할
대상이 없다. 관제사가 의견에 동의하지 않으면 `note` 에 적는다 — 그게 다음 판정
규칙을 고칠 근거가 된다.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.models.assessment_history import AssessmentLevel

router = APIRouter(prefix="/approvals", tags=["approvals"])


class PendingItem(BaseModel):
    """아직 관제사가 확인하지 않은 판정 1건."""

    id: int
    call_sign: str
    vessel_name: str | None
    stage: str = Field(description="입항전 | 접안직전 | 하역중")
    wharf_name: str | None
    level: str = Field(description="적합 | 주의 | 부적합 | 판정불가")
    changed_from: str | None = Field(default=None, description="직전 판정 등급. 값이 있으면 등급이 바뀐 것")
    action: str | None = Field(default=None, description="조치안. 우리가 실행하지 않는다")
    recipient: str | None = Field(default=None, description="조치안을 받을 곳 — 선석운영주체 | VTS | 터미널")
    reasons: list[str]
    assessed_at_utc: datetime


_QUERY_PENDING = text("""
    SELECT id, call_sign, vessel_name, stage, wharf_name, level,
           changed_from, action, recipient, reasons, assessed_at_utc
    FROM assessment_history
    WHERE acknowledged_at_utc IS NULL
      AND (NOT :only_actionable OR level <> :fit_level)
    ORDER BY assessed_at_utc DESC
    LIMIT :limit
""")


@router.get(
    "/pending",
    response_model=list[PendingItem],
    summary="확인 대기 판정 목록 (승인 대기가 아니다)",
)
async def get_pending(
    only_actionable: bool = Query(
        True, description="'적합' 판정을 빼고 관제사가 볼 필요가 있는 것만(기본값)",
    ),
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_session),
) -> list[PendingItem]:
    """아직 확인되지 않은 판정.

    **이 목록은 처리해야 할 작업 대기열이 아니다.** 여기 있는 항목을 확인하지
    않아도 아무 자원도 잠기지 않고 아무 배도 기다리지 않는다. 관제사가 자기
    판단에 확신을 더하려고 보는 목록이다.
    """
    rows = (
        await db.execute(
            _QUERY_PENDING,
            {
                "only_actionable": only_actionable,
                "fit_level": AssessmentLevel.FIT.value,
                "limit": limit,
            },
        )
    ).mappings().all()
    return [PendingItem(**dict(r)) for r in rows]


class AcknowledgeRequest(BaseModel):
    acknowledged_by: str = Field(
        description="확인한 관제사 식별자(자유 텍스트 — 인증 체계 없음, 07 문서 §8.6 한계 그대로)"
    )
    note: str | None = Field(
        default=None,
        description="관제사 의견. 판정에 동의하지 않는다면 여기 적는다 — 규칙을 고칠 근거가 된다",
    )


class AcknowledgeResult(BaseModel):
    id: int
    acknowledged_by: str
    acknowledged_at_utc: datetime
    note: str | None = None


_QUERY_ACK = text("""
    UPDATE assessment_history
    SET acknowledged_by = :acknowledged_by,
        acknowledged_at_utc = :now,
        reasons = CASE
            WHEN :note IS NULL THEN reasons
            ELSE reasons || ARRAY[:note_line]::text[]
        END
    WHERE id = :id AND acknowledged_at_utc IS NULL
    RETURNING id, acknowledged_by, acknowledged_at_utc
""")

_QUERY_EXISTS = text("SELECT acknowledged_by FROM assessment_history WHERE id = :id")


@router.post(
    "/{assessment_id}/acknowledge",
    response_model=AcknowledgeResult,
    summary="판정 확인 기록 (아무것도 확정되지 않는다)",
)
async def acknowledge(
    assessment_id: int, body: AcknowledgeRequest, db: AsyncSession = Depends(get_session),
) -> AcknowledgeResult:
    now = datetime.now(timezone.utc)
    note_line = f"[관제사 {body.acknowledged_by}] {body.note}" if body.note else None

    row = (
        await db.execute(
            _QUERY_ACK,
            {
                "id": assessment_id, "acknowledged_by": body.acknowledged_by,
                "now": now, "note": body.note, "note_line": note_line,
            },
        )
    ).mappings().first()

    if row is None:
        # 없는 id 인지, 이미 확인된 건인지 구분해서 알려준다.
        existing = (await db.execute(_QUERY_EXISTS, {"id": assessment_id})).first()
        await db.rollback()
        if existing is None:
            raise HTTPException(status_code=404, detail=f"판정 id={assessment_id}를 찾을 수 없습니다.")
        raise HTTPException(
            status_code=409,
            detail=f"이미 '{existing[0]}'님이 확인한 판정입니다. 목록을 새로고침하세요.",
        )

    await db.commit()
    return AcknowledgeResult(
        id=row["id"], acknowledged_by=row["acknowledged_by"],
        acknowledged_at_utc=row["acknowledged_at_utc"], note=body.note,
    )
