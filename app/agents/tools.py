"""도구 계층 — 감독자(오케스트레이터)와 챗봇이 함께 부르는 판단 단위 (27번 설계안 A단계).

각 에이전트 서비스의 판단 함수를 **입력과 출력이 고정된 함수**로 한곳에 모은다.
새 판단 로직은 여기 두지 않는다 — 감싸는 함수의 동작을 그대로 돌려준다.

왜 한곳에 모으나:
  · 오케스트레이터는 지금 에이전트 서비스 네 곳을 직접 부른다. 감독자 루프(B단계)의
    보강·교차 확인과 챗봇 도구 호출(C단계)이 같은 입구를 써야, 판정 잡과 챗봇이
    같은 질문에 다른 근거로 답하는 일이 생기지 않는다.
  · 튜플로 돌려주던 결과(선석 확인·대체안)에 이름을 붙여, 부르는 쪽이 자리 순서가
    아니라 필드 이름으로 읽게 한다.

도구 목록:
  check_weather       기상 — 현재 관측 + 체류 종료 시각까지 예보 경고
  check_berth         선석 — 정해진 선석 하나의 가용수심·흘수 여유·이웃 화물(검증모드)
  check_segregation   혼재 — 이웃 화물과의 규칙 하한 + 설명(LLM)
  suggest_alternatives 대체안 — 배정이 아니라 제안
  find_candidates     [탐색모드] 카테고리 기준 후보 top-N   — D단계에서 정리 예정
  resolve_assignment  [탐색모드] 점유 시 대체·정박지 재탐색 — D단계에서 정리 예정
"""

from datetime import datetime

from neo4j import AsyncDriver
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import (
    AdjacentCargo,
    CargoRef,
    SafetyAssessmentRequest,
    SafetyAssessmentResult,
)
from app.agents.safety.service import assess_safety
from app.agents.scheduling.schemas import (
    BerthCandidate,
    BerthResolution,
    SchedulingRequest,
    SchedulingResult,
    VesselSpec,
)
from app.agents.scheduling.service import (
    build_candidate_for_wharf_name,
    find_berth_candidates,
    resolve_berth_assignment,
    suggest_alternative_berths,
)
from app.agents.weather.schemas import WeatherAssessmentRequest, WeatherAssessmentResult
from app.agents.weather.service import assess_weather
from app.llm.base import LLMClient


class BerthCheck(BaseModel):
    """check_berth 결과. candidate 가 None 이면 확인 실패이고 reason 이 이유다."""

    candidate: BerthCandidate | None
    reason: str | None = None
    evidence_missing: bool = Field(
        default=False,
        description="확인 실패가 근거 부족(표기 미해소·수심/조위 자료 없음)인가, 실제 부적합인가",
    )


class Alternatives(BaseModel):
    """suggest_alternatives 결과. 배정이 아니라 관제사에게 보이는 제안이다."""

    candidates: list[BerthCandidate] = Field(default_factory=list)
    note: str | None = None


async def check_weather(
    db: AsyncSession,
    *,
    window_end: datetime,
    wharf_name: str | None = None,
    berth_group: str | None = None,
    as_of: datetime | None = None,
) -> WeatherAssessmentResult:
    """선석(또는 전역 기본) 기상 임계값으로 지금 작업 가능 여부를 판정한다.

    wharf_name 을 주면 외해 부이 파고를 항내 부두에 대입하지 않는다.
    window_end 까지의 예보는 경고(forecast_warning)로만 붙는다 — 등급에는 반영하지 않는다.
    """
    return await assess_weather(
        db,
        WeatherAssessmentRequest(
            berth_group=berth_group,
            wharf_name=wharf_name,
            as_of=as_of,
            expected_completion_at=window_end,
        ),
    )


async def check_berth(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    *,
    wharf_name: str,
    vessel: VesselSpec,
    window_start: datetime,
    window_end: datetime,
    draught_margin_m: float = 1.0,
) -> BerthCheck:
    """정해진 선석 하나가 이 배에 맞는가(검증모드). 이웃 화물도 함께 채운다."""
    candidate, reason, evidence_missing = await build_candidate_for_wharf_name(
        db,
        neo4j_driver,
        wharf_name=wharf_name,
        vessel=vessel,
        window_start=window_start,
        window_end=window_end,
        draught_margin_m=draught_margin_m,
    )
    return BerthCheck(candidate=candidate, reason=reason, evidence_missing=evidence_missing)


async def check_segregation(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    llm_client: LLMClient,
    *,
    cargo: CargoRef,
    cargos: list[CargoRef],
    adjacent_cargos: list[AdjacentCargo],
) -> SafetyAssessmentResult:
    """이 배의 화물과 이웃 화물의 혼재 등급. 등급은 규칙 하한이 정하고 LLM 은 설명만 쓴다."""
    return await assess_safety(
        db,
        neo4j_driver,
        llm_client,
        SafetyAssessmentRequest(
            target_cargo=cargo, target_cargos=cargos, adjacent_cargos=adjacent_cargos,
        ),
    )


async def suggest_alternatives(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    *,
    cargo: CargoRef,
    cargos: list[CargoRef],
    vessel: VesselSpec,
    window_start: datetime,
    window_end: datetime,
    exclude_wharf_name: str | None,
    draught_margin_m: float = 1.0,
) -> Alternatives:
    """지금 선석이 맞지 않을 때 관제사에게 보일 대체 선석(가까운 순). 배정하지 않는다."""
    candidates, note = await suggest_alternative_berths(
        db,
        neo4j_driver,
        cargo=cargo,
        vessel=vessel,
        window_start=window_start,
        window_end=window_end,
        exclude_wharf_name=exclude_wharf_name,
        draught_margin_m=draught_margin_m,
        extra_cargos=cargos,
    )
    return Alternatives(candidates=candidates, note=note)


async def find_candidates(
    db: AsyncSession, neo4j_driver: AsyncDriver, request: SchedulingRequest,
) -> SchedulingResult:
    """[탐색모드] 화물 카테고리·수심 기준 후보 선석. 배정 가정 경로라 D단계에서 정리한다."""
    return await find_berth_candidates(db, neo4j_driver, request)


async def resolve_assignment(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    *,
    candidate: BerthCandidate,
    vessel: VesselSpec,
    window_start: datetime,
    window_end: datetime,
    category: str | None,
) -> BerthResolution:
    """[탐색모드] 점유 시 전용 → 대체 → 정박지 재탐색. 배정 경로라 D단계에서 정리한다."""
    return await resolve_berth_assignment(
        db,
        neo4j_driver,
        candidate=candidate,
        vessel=vessel,
        window_start=window_start,
        window_end=window_end,
        category=category,
    )
