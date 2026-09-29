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
  where_is            운영 — 이 배는 지금 어디에 있고 무엇을 실었나, 최근 판정(C단계)
  who_is_at           운영 — 이 부두와 인접 부두에 붙은 배·화물·최근 판정(C단계)

도구 결과는 berth_opinion · weather_opinion · segregation_opinion 으로 의견(Opinion:
등급 · 확인한 것 · 못 본 것)이 된다(B단계).
"""

from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from typing import Literal

from neo4j import AsyncDriver
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.schemas import (
    AdjacentCargo,
    CargoRef,
    RiskLevel,
    SafetyAssessmentRequest,
    SafetyAssessmentResult,
)
from app.agents.safety.service import assess_safety
from app.agents.scheduling.schemas import BerthCandidate, VesselSpec
from app.agents.scheduling.service import build_candidate_for_wharf_name, suggest_alternative_berths
from app.agents.weather.schemas import WeatherAssessmentRequest, WeatherAssessmentResult, WorkStatus
from app.agents.weather.service import assess_weather
from app.llm.base import LLMClient


OpinionLevel = Literal["적합", "주의", "부적합", "판정불가"]


class Opinion(BaseModel):
    """도구 하나의 의견 — 등급과 함께 **무엇을 확인했고 무엇을 못 봤는지**를 남긴다 (B단계).

    등급만 넘기면 '확인해서 괜찮다'와 '볼 근거가 없었다'가 같은 '적합'으로 보인다.
    missing 이 비어 있지 않은 적합은 관제사가 그 빈칸을 알고 읽어야 한다.
    """

    axis: Literal["선석", "기상", "혼재"]
    level: OpinionLevel
    evidence: list[str] = Field(default_factory=list, description="등급의 근거 문장")
    checked: list[str] = Field(default_factory=list, description="확인한 것")
    missing: list[str] = Field(default_factory=list, description="못 본 것")
    notes: list[str] = Field(
        default_factory=list,
        description="[2026-09-29] 참고 — 판정에 쓰지 않은 값(외해 파고 등). '확인한 것' 칩에 두면 확인한 근거처럼 읽혔다",
    )


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
    draught_margin_m: float | None = None,
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
    call_sign: str | None = None,
    defer: list[Awaitable[None]] | None = None,
) -> SafetyAssessmentResult:
    """이 배의 화물과 이웃 화물의 혼재 등급. 등급은 규칙 하한이 정하고 LLM 은 설명만 쓴다.

    call_sign 을 주면 이번 입항 건 화물 신고의 하역방식을 채운다(안전 에이전트가 조회, 2026-09-29).
    defer 를 주면 LLM 서술을 기다리지 않고 그 목록에 넣는다(assess_safety 참고).
    """
    return await assess_safety(
        db,
        neo4j_driver,
        llm_client,
        SafetyAssessmentRequest(
            target_cargo=cargo, target_cargos=cargos, adjacent_cargos=adjacent_cargos, call_sign=call_sign,
        ),
        defer=defer,
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
    draught_margin_m: float | None = None,
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


# ─────────────────────────────────────────────────────────────────────────────
# 의견(Opinion) — 도구 결과를 등급 · 확인한 것 · 못 본 것으로 옮긴다 (B단계)
#
# 판단을 새로 하지 않는다. 도구가 이미 낸 결과를 감독자·판정 기록·챗봇이 같은
# 모양으로 읽게 할 뿐이다.
# ─────────────────────────────────────────────────────────────────────────────
_KST = timezone(timedelta(hours=9))


def berth_opinion(check: BerthCheck) -> Opinion:
    """check_berth 결과 → 선석 의견."""
    c = check.candidate
    if c is None:
        if check.evidence_missing:
            return Opinion(axis="선석", level="판정불가", missing=[check.reason or "선석 근거 없음"])
        return Opinion(axis="선석", level="부적합", evidence=[check.reason or "선석 조건 불충족"])
    neighbors = {a.berth_name for a in c.adjacent_cargos}
    return Opinion(
        axis="선석",
        level="적합",
        evidence=[f"'{c.wharf_name}' 흘수 여유 {c.draught_margin_m:.2f}m (해도 수심 + 체류 중 최저 조위)"],
        checked=["가용수심·조위", f"이웃 선석 {len(neighbors)}곳의 재항 화물"],
    )


def weather_opinion(w: WeatherAssessmentResult) -> Opinion:
    """check_weather 결과 → 기상 의견. 예보는 등급을 바꾸지 않고 근거에만 남긴다."""
    level: OpinionLevel = (
        "적합" if w.status is WorkStatus.NORMAL
        else "판정불가" if w.status is WorkStatus.UNKNOWN
        else "부적합"
    )
    checked, missing = [], []
    # [2026-09-29] missing 은 화면의 '확인 필요' 줄이 된다 — '대상 — 이유'로 적는다.
    if w.wind.is_stale:
        missing.append("풍속 — 관측이 끊겼거나 오래됐습니다")
    else:
        checked.append(f"풍속 관측 {w.wind.value}{w.wind.unit}")
    # 파고는 항내 부두에 적용하지 않는 값이라 없다고 '못 본 것'으로 적지 않는다. 적용되는
    # 부두에서 없으면 등급이 이미 판정불가이고 사유가 reasons 에 남는다.
    # [2026-09-29] '적용하지 않음 - 참고값' 문장은 등급의 근거가 아니다 — 근거에서 빼고 확인한 것으로
    # 옮긴다. 예전엔 항내 부두 판정마다 근거 첫머리에 "파고 … 적용하지 않음"이 떴다(31건 중 30건).
    evidence = [r for r in w.reasons if "적용하지 않음" not in r]
    wave_reference = [r for r in w.reasons if "적용하지 않음" in r]
    # 판정에 쓴 파고만 '파고 관측'으로 적는다. 안 쓴 파고까지 적으면 '확인' 칩에 "파고 관측 1.4m"와
    # "파고 1.4m … 적용하지 않음"이 나란히 떠, 파고를 봤다는 건지 안 봤다는 건지 읽히지 않았다.
    if not w.wave.is_stale and not wave_reference:
        checked.append(f"파고 관측 {w.wave.value}{w.wave.unit}")
    fw = w.forecast_warning
    if fw is not None:
        checked.append(f"체류 종료까지 예보 {fw.forecast_points_checked}개 시각")
        if fw.will_deteriorate and fw.earliest_deterioration_at_utc is not None:
            at = fw.earliest_deterioration_at_utc.astimezone(_KST)
            cause = f" ({' · '.join(fw.earliest_deterioration_causes)})" if fw.earliest_deterioration_causes else ""
            evidence.append(f"{at:%m/%d %H:%M} 부터 '{fw.worst_status.value}' 예보{cause}")
        elif level == "적합":
            evidence.append("체류 중 악화 예보 없음")
        if fw.no_forecast_after_utc is not None:
            gap = fw.no_forecast_after_utc.astimezone(_KST)
            missing.append(f"{gap:%m/%d %H:%M} 이후 기상 — 예보가 아직 나오지 않았습니다")
    return Opinion(axis="기상", level=level, evidence=evidence, checked=checked, missing=missing,
                   notes=wave_reference)


def segregation_opinion(s: SafetyAssessmentResult) -> Opinion:
    """check_segregation 결과 → 혼재 의견."""
    if s.risk_level is RiskLevel.SAFE:
        level: OpinionLevel = "적합"
    elif s.risk_level is RiskLevel.BLOCKED:
        level = "부적합"
    else:
        level = "주의"
    # [2026-09-29] 근거는 안전 에이전트가 코드로 만든 verdict_basis 를 그대로 쓴다. 예전엔 이웃 충돌만
    # 옮겨, 같은 선박 화물 충돌로 '주의'가 된 배의 근거가 비어 있었다(실측 GOLDEN DENISE).
    return Opinion(
        axis="혼재",
        level=level,
        evidence=list(s.verdict_basis),
        checked=[f"화물 {len(s.cargo_verdicts) or 1}종 × 이웃 화물 {s.adjacent_count}건, 규칙 하한 '{s.rule_engine_floor.value}'"],
        missing=list(s.needs_check),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 운영 도구 — 지금 어느 배가 어디에 있고 무엇을 실었나 (C단계, 챗봇이 부른다)
#
# 판정 잡과 같은 뷰(mart.vessel_presence · vessel_current_call · cargo_msds)와 같은
# 인접 그래프(ADJACENT_TO)를 읽는다 — 챗봇이 판정 잡과 다른 사실을 말하지 않게.
# ─────────────────────────────────────────────────────────────────────────────
class VesselNow(BaseModel):
    call_sign: str | None
    vessel_name: str | None
    zone: str = Field(description="BERTH(접안) | ANCHORAGE(정박지) | STOPPED | UNDERWAY")
    place: str | None = Field(default=None, description="접안 부두 또는 정박지 이름")
    speed_kn: float | None = None
    position_at_utc: datetime | None = None
    cargos: list[str] = Field(default_factory=list, description="현재 입항 건의 화물(합성)")
    chem_ids: list[str] = Field(default_factory=list)
    latest_level: str | None = Field(default=None, description="가장 최근 판정 등급")
    latest_reason: str | None = None
    latest_at_utc: datetime | None = None


class BerthNow(BaseModel):
    wharf_name: str
    vessels: list[VesselNow] = Field(default_factory=list)
    neighbors: list["NeighborNow"] = Field(default_factory=list)


class NeighborNow(BaseModel):
    wharf_name: str
    distance_m: float | None = None
    vessels: list[VesselNow] = Field(default_factory=list)


BerthNow.model_rebuild()

_SQL_PRESENCE = """
    SELECT vp.callsgn, vp.vessel_name, vp.presence_zone, vp.berth_name, vp.anchorage_name,
           vp.sog, vp.received_at_utc,
           ARRAY(SELECT DISTINCT coalesce(cm.msds_name_ko, cm.cargo_name_raw)
                 FROM mart.vessel_current_call vc
                 JOIN mart.cargo_msds cm ON cm.port_call_key = vc.port_call_key
                 WHERE vc.callsgn = upper(btrim(vp.callsgn))) AS cargos,
           ARRAY(SELECT DISTINCT cm.chem_id
                 FROM mart.vessel_current_call vc
                 JOIN mart.cargo_msds cm ON cm.port_call_key = vc.port_call_key
                 WHERE vc.callsgn = upper(btrim(vp.callsgn)) AND cm.chem_id IS NOT NULL) AS chem_ids,
           ah.level, ah.reasons[1] AS reason, ah.assessed_at_utc
    FROM mart.vessel_presence vp
    LEFT JOIN LATERAL (
        SELECT level, reasons, assessed_at_utc FROM assessment_history a
        WHERE a.call_sign = upper(btrim(vp.callsgn))
        ORDER BY assessed_at_utc DESC LIMIT 1
    ) ah ON true
"""


def _vessel_now(row) -> VesselNow:
    return VesselNow(
        call_sign=row["callsgn"], vessel_name=row["vessel_name"], zone=row["presence_zone"],
        place=row["berth_name"] or row["anchorage_name"],
        speed_kn=float(row["sog"]) if row["sog"] is not None else None,
        position_at_utc=row["received_at_utc"],
        cargos=[c for c in row["cargos"] if c], chem_ids=list(row["chem_ids"]),
        latest_level=row["level"], latest_reason=row["reason"], latest_at_utc=row["assessed_at_utc"],
    )


def _norm_place(name: str) -> str:
    return "".join(name.lower().split()).replace("-", "")


async def where_is(db: AsyncSession, *, query: str) -> list[VesselNow]:
    """호출부호 또는 선박명(일부)으로 배를 찾아 지금 위치·화물·최근 판정을 돌려준다."""
    rows = (await db.execute(text(_SQL_PRESENCE + """
        WHERE upper(btrim(vp.callsgn)) = upper(btrim(:q))
           OR replace(upper(vp.vessel_name), ' ', '') LIKE '%' || replace(upper(:q), ' ', '') || '%'
        ORDER BY vp.received_at_utc DESC
        LIMIT 5
    """), {"q": query})).mappings().all()
    return [_vessel_now(r) for r in rows]


async def who_is_at(db: AsyncSession, neo4j_driver: AsyncDriver, *, wharf: str) -> BerthNow | None:
    """부두에 지금 붙어 있는 배와, 인접 부두(ADJACENT_TO)에 붙어 있는 배를 돌려준다.

    부두 이름은 표기가 흔들리므로(S-OIL2부두 · S-Oil 2부두) 공백·하이픈·대소문자를
    무시하고 그래프의 부두명과 맞춘다. 못 찾으면 None.
    """
    async with neo4j_driver.session() as s:
        names = [r["w"] for r in await (await s.run(
            "MATCH (b:Berth) WHERE b.wharf_name IS NOT NULL RETURN DISTINCT b.wharf_name AS w"
        )).data()]
    key = _norm_place(wharf)
    matched = next((n for n in names if _norm_place(n) == key), None) \
        or next((n for n in names if key in _norm_place(n)), None)
    if matched is None:
        return None

    async with neo4j_driver.session() as s:
        neighbor_rows = await (await s.run("""
            MATCH (b:Berth {wharf_name: $w})-[r:ADJACENT_TO]->(n:Berth)
            WHERE n.wharf_name <> $w
            RETURN n.wharf_name AS wharf, min(r.distance_m) AS distance_m
            ORDER BY distance_m
        """, w=matched)).data()

    wanted = [matched] + [r["wharf"] for r in neighbor_rows]
    rows = (await db.execute(text(_SQL_PRESENCE + """
        WHERE vp.presence_zone = 'BERTH' AND vp.berth_name = ANY(:w)
    """), {"w": wanted})).mappings().all()
    by_wharf: dict[str, list[VesselNow]] = {}
    for r in rows:
        by_wharf.setdefault(r["berth_name"], []).append(_vessel_now(r))

    return BerthNow(
        wharf_name=matched,
        vessels=by_wharf.get(matched, []),
        neighbors=[
            NeighborNow(wharf_name=r["wharf"], distance_m=r["distance_m"], vessels=by_wharf.get(r["wharf"], []))
            for r in neighbor_rows
        ],
    )
