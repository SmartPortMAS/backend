from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import tools
from app.agents.safety.schemas import AdjacentCargo, CargoRef
from app.agents.scheduling.schemas import VesselSpec
from app.agents.scheduling.service import adjacent_cargos_for_wharf
from app.core.deps import get_session
from app.core.exceptions import MsdsNotFoundError, MsdsUpstreamError
from app.neo4j_client import neo4j_client

router = APIRouter(prefix="/scheduling", tags=["scheduling"])

_DESCRIPTION = """
이 배·화물에 맞는 **대체 선석 제안**(최대 3)을 반환합니다. LLM을 쓰지 않는 결정적 판단입니다.

**배정이 아니라 제안입니다.** 어떤 자리도 잠그지 않고, 실제로 옮길지는 선석회의·VTS·터미널이
정합니다. 판정 잡이 '부적합'일 때 판정 기록에 붙이는 대체안과 같은 계산입니다.

- 수심 조건: 해도 수심 + 체류 중 최저 조위 − 흘수 ≥ 필요 여유 max(1.0m, 흘수 10%).
  판정(검증모드)·대시보드 흘수 점검과 같은 규칙입니다.
- 화물 조건: 함께 실은 화물 카테고리를 모두 취급하는 선석만.
- `exclude_wharf_name`(지금 선석)은 뺍니다. 점유는 탈락이 아니라 정렬(빈 자리 먼저)에만 씁니다.
- 후보가 없으면 `candidates` 가 비고 `note` 에 이유가 옵니다(조위 예보 없음 등).

[2026-09-27] 예전의 `POST /scheduling/candidates`(카테고리 기준 top-3, 조위 미반영)를 대체했습니다
— 선석을 새로 고르는 탐색모드를 걷어냈습니다(27번 설계안 D단계).
"""

_RESPONSES: dict = {
    404: {"description": "요청한 화물의 MSDS를 찾을 수 없음 (KOSHA 미등재)"},
    502: {"description": "KOSHA MSDS API 호출 실패. 재시도 가능"},
}


class AlternativesRequest(BaseModel):
    vessel: VesselSpec
    cargo: CargoRef
    cargos: list[CargoRef] = Field(default_factory=list, description="같은 입항 건의 나머지 화물")
    window_start: datetime
    window_end: datetime
    exclude_wharf_name: str | None = Field(default=None, description="지금 선석(제안에서 뺀다)")

    @model_validator(mode="after")
    def _window_must_be_ordered(self) -> "AlternativesRequest":
        if self.window_end <= self.window_start:
            raise ValueError("window_end는 window_start보다 이후여야 합니다.")
        return self


@router.post(
    "/alternatives",
    response_model=tools.Alternatives,
    summary="대체 선석 제안",
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
async def alternatives(
    request: AlternativesRequest,
    db: AsyncSession = Depends(get_session),
) -> tools.Alternatives:
    try:
        return await tools.suggest_alternatives(
            db, neo4j_client.driver,
            cargo=request.cargo, cargos=request.cargos, vessel=request.vessel,
            window_start=request.window_start, window_end=request.window_end,
            exclude_wharf_name=request.exclude_wharf_name,
        )
    except MsdsNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"MSDS not found for identifier: {e.identifier}")
    except MsdsUpstreamError as e:
        raise HTTPException(status_code=502, detail=f"KOSHA MSDS API 호출 실패: {e.reason}")


class AdjacentCargos(BaseModel):
    wharf_name: str | None = Field(description="정본 선석명. 선석 제원 마스터에서 못 찾으면 None")
    adjacent_cargos: list[AdjacentCargo] = Field(
        default_factory=list, description="인접 선석에 지금 있는 화물 — /safety/verdict·assess 의 adjacent_cargos 에 그대로 넣는다",
    )


@router.get(
    "/adjacent-cargos",
    response_model=AdjacentCargos,
    summary="선석의 이웃 화물 (혼재 판정 입력)",
)
async def adjacent_cargos(
    wharf_name: str = Query(description="배가 있는 선석(위치 판정 선석명 또는 PORT-MIS 표기)"),
    call_sign: str | None = Query(default=None, description="판정 대상 배 — 자기 화물은 이웃에서 뺀다"),
    db: AsyncSession = Depends(get_session),
) -> AdjacentCargos:
    """판정 잡(10분)과 같은 인접 계산 — 그래프 ADJACENT_TO + 지금 재항 화물.

    [2026-09-27] 선박 상세 패널이 프론트엔드 자체 인접표로 이웃을 구해 그 표 밖 선석은
    이웃 0건으로 판정받던 것을 이 경로로 바꿨다.
    """
    canonical, cargos = await adjacent_cargos_for_wharf(
        db, neo4j_client.driver, wharf_name=wharf_name, exclude_call_sign=call_sign,
    )
    return AdjacentCargos(wharf_name=canonical, adjacent_cargos=cargos)
