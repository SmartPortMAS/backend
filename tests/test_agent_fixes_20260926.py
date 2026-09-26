"""2026-09-26 에이전트 결함 수정 회귀 테스트.

번호는 분석 보고의 항목 번호다. DB·Neo4j 를 쓰는 테스트는 로컬 개발 DB 를 읽기만
하고(6번은 쓰되 롤백), 데이터가 조건을 못 갖추면 skip 한다 — 없는 조건을 지어내지 않는다.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.agents.orchestrator import service as orch
from app.agents.orchestrator.schemas import OrchestratorRequest, OrchestratorResult, OverallDecision
from app.agents.safety.schemas import AdjacentCargo, CargoRef, RiskLevel, SafetyAssessmentRequest
from app.agents.safety.service import assess_verdict
from app.agents.scheduling import service as sched
from app.agents.scheduling.occupancy import find_overlapping_reservations
from app.agents.scheduling.schemas import BerthCandidate, OccupancyStatus, VesselSpec
from app.agents.weather.schemas import ObservationFactor, WeatherAssessmentResult, WorkStatus
from app.models.assessment_history import AssessmentLevel, AssessmentStage
from app.services.assessment import level_from_decision, record_assessment

NOW = datetime.now(timezone.utc)


def _weather(status: WorkStatus) -> WeatherAssessmentResult:
    obs = ObservationFactor(value=None, unit="m/s", is_stale=True)
    return WeatherAssessmentResult(
        status=status, assessed_at_utc=NOW, wind=obs, wave=obs, reasons=[f"테스트 {status.value}"]
    )


def _candidate() -> BerthCandidate:
    return BerthCandidate(
        rank=1, berth_id="효성부두", wharf_name="효성부두", port_name="온산항", depth_m=12.0,
        draught_margin_m=3.0, occupancy_status=OccupancyStatus.AVAILABLE,
    )


class _NoLLM:
    async def generate_structured(self, **_):
        raise AssertionError("이 경로는 LLM 을 부르면 안 된다")


def _verify_request() -> OrchestratorRequest:
    return OrchestratorRequest(
        vessel=VesselSpec(draught_m=9.0, call_sign="TEST"),
        cargo=CargoRef(chem_id="001008"),
        window_start=NOW, window_end=NOW + timedelta(hours=24),
        assigned_wharf_name="효성부두",
    )


# ── 1. 검증모드 기상 판단불가 → 판정불가(조치 없음), 하역중단 → 기상 사유 부적합 ──

@pytest.mark.asyncio
async def test_1_weather_unknown_is_evidence_missing_not_unfit(monkeypatch):
    async def fake_candidate(*a, **k):
        return _candidate(), None, False

    async def fake_weather(db, req):
        return _weather(WorkStatus.UNKNOWN)

    monkeypatch.setattr(orch, "build_candidate_for_wharf_name", fake_candidate)
    monkeypatch.setattr(orch, "assess_weather", fake_weather)

    result = await orch.orchestrate(None, None, _NoLLM(), _verify_request())
    level, _ = level_from_decision(result)
    assert result.evidence_missing is True
    assert level is AssessmentLevel.UNKNOWN


@pytest.mark.asyncio
async def test_1_weather_stop_is_weather_blocked(monkeypatch):
    async def fake_candidate(*a, **k):
        return _candidate(), None, False

    async def fake_weather(db, req):
        return _weather(WorkStatus.STOP)

    async def fake_alternatives(*a, **k):
        return [], "테스트"

    monkeypatch.setattr(orch, "build_candidate_for_wharf_name", fake_candidate)
    monkeypatch.setattr(orch, "assess_weather", fake_weather)
    monkeypatch.setattr(orch, "suggest_alternative_berths", fake_alternatives)

    result = await orch.orchestrate(None, None, _NoLLM(), _verify_request())
    level, headline = level_from_decision(result)
    assert result.overall_decision is OverallDecision.WEATHER_BLOCKED
    assert level is AssessmentLevel.UNFIT
    assert "기상" in headline


# ── 2. 점유: 선석 ID 로 물어도 다선석 부두의 실제 접안이 보여야 한다 ──

@pytest.mark.asyncio
async def test_2_occupancy_by_berth_id(db):
    row = (await db.execute(text("""
        SELECT vp.berth_name, array_agg(DISTINCT b.berth_id) AS ids
        FROM mart.vessel_presence vp JOIN berth b ON b.wharf_name = vp.berth_name
        WHERE vp.presence_zone = 'BERTH'
        GROUP BY vp.berth_name HAVING count(DISTINCT b.berth_id) > 1
        LIMIT 1
    """))).mappings().first()
    if row is None:
        pytest.skip("지금 접안 선박이 있는 다선석 부두가 없다")
    got = await find_overlapping_reservations(db, berth_ids=list(row["ids"]), window_start=NOW, window_end=NOW)
    assert set(got) == set(row["ids"]), f"{row['berth_name']} 선석 모두 점유로 보여야 함: {got.keys()}"


# ── 3(a). 인접 화물에 검증 대상 배 자신의 화물이 섞이면 안 된다 ──

@pytest.mark.asyncio
async def test_3a_own_cargo_excluded(db):
    row = (await db.execute(text("""
        SELECT fa.wharf_name, bcc.callsgn
        FROM mart.berth_current_cargo bcc
        JOIN mart.facility_alias fa ON fa.source_name = bcc.facility_name AND fa.facility_type = 'BERTH'
        WHERE bcc.chem_id IS NOT NULL
        GROUP BY 1, 2
        LIMIT 1
    """))).mappings().first()
    if row is None:
        pytest.skip("재항 화물이 없다")
    expected = {r[0] for r in (await db.execute(text("""
        SELECT DISTINCT bcc.chem_id
        FROM mart.berth_current_cargo bcc
        JOIN mart.facility_alias fa ON fa.source_name = bcc.facility_name AND fa.facility_type = 'BERTH'
        WHERE bcc.chem_id IS NOT NULL AND fa.wharf_name = :w
          AND upper(btrim(bcc.callsgn)) <> upper(btrim(:cs))
    """), {"w": row["wharf_name"], "cs": row["callsgn"]})).all()}
    got = await sched._real_adjacent_cargo_by_wharf(
        db, [row["wharf_name"]], exclude_call_sign=row["callsgn"]
    )
    assert {c["chem_id"] for c in got.get(row["wharf_name"], [])} == expected


# ── 3(b). 비어 있는 이웃 부두에는 대표 화물을 넣지 않는다(배는 있고 화물 미상이면 넣는다) ──

def test_3b_vacant_neighbor_has_no_representative_cargo():
    entries = [
        {"adjacent_berth_id": "빈부두-1선석", "adjacent_wharf_name": "빈부두", "distance_m": 100.0, "categories": ["유류"]},
        {"adjacent_berth_id": "찬부두-1선석", "adjacent_wharf_name": "찬부두", "distance_m": 100.0, "categories": ["유류"]},
    ]
    got = sched._adjacent_cargos_for(entries, real_cargo_by_wharf={}, occupied_wharfs={"찬부두"})
    assert [a.berth_name for a in got] == ["찬부두-1선석"]


# ── 4. MSDS 배정불가(충돌 2건 이상)는 화물쌍 단위 규칙이다 ──

def _c(chem, cat, direction="target_incompatible_with_adjacent"):
    return {"chem_id": chem, "category": cat, "direction": direction}


def test_4_floor_is_per_adjacent_pair():
    from app.agents.safety.rule_engine import compute_risk_floor_by_adjacent

    assert compute_risk_floor_by_adjacent([_c("B1", "가연성물질"), _c("B2", "가연성물질")]) is RiskLevel.DANGER
    assert compute_risk_floor_by_adjacent(
        [_c("B1", "가연성물질"), _c("B1", "산", "adjacent_incompatible_with_target")]
    ) is RiskLevel.BLOCKED
    assert compute_risk_floor_by_adjacent([]) is RiskLevel.SAFE


@pytest.mark.asyncio
async def test_4_real_case_two_neighbors_stay_danger(db, neo4j):
    # 황산 vs 가솔린 / 톨루엔 — 각각 단독이면 '위험'(MSDS 가연성물질 기피, 2026-09-27 실측).
    # [2026-09-27] 예전 예시(에틸렌글리콜 vs 에탄올·에틸렌)의 '위험'은 MSDS 상투 문구가
    # 만든 가짜 충돌이라 그래프 정리(msds_neo4j_loader.prune_incompatible_noise)로 사라졌다.
    req = SafetyAssessmentRequest(
        target_cargo=CargoRef(chem_id="001049"),
        adjacent_cargos=[
            AdjacentCargo(berth_name="이웃1", cargo=CargoRef(chem_id="016420")),
            AdjacentCargo(berth_name="이웃2", cargo=CargoRef(chem_id="001032")),
        ],
    )
    v = await assess_verdict(db, neo4j, req)
    assert v.risk_level is RiskLevel.DANGER


# ── 5. 적합 귀결이라도 혼재 위험·주의면 판정은 '주의' ──

def _approved_with(risk: RiskLevel) -> OrchestratorResult:
    from app.agents.safety.schemas import SafetyAssessmentResult

    safety = SafetyAssessmentResult.model_construct(
        risk_level=risk, rule_engine_floor=risk, target_cargo_name="t",
    )
    return OrchestratorResult.model_construct(
        overall_decision=OverallDecision.APPROVED, weather_assessment=_weather(WorkStatus.NORMAL),
        safety_assessment=safety, assignment_trace=[], evidence_missing=False,
    )


@pytest.mark.parametrize("risk,expected", [
    (RiskLevel.SAFE, AssessmentLevel.FIT),
    (RiskLevel.CAUTION, AssessmentLevel.CAUTION),
    (RiskLevel.DANGER, AssessmentLevel.CAUTION),
])
def test_5_safety_level_reflected(risk, expected):
    level, _ = level_from_decision(_approved_with(risk))
    assert level is expected


# ── 6. 등급이 같아도 대체 선석 제안이 바뀌면 기록한다 ──

@pytest.mark.asyncio
async def test_6_suggestion_change_is_recorded(db):
    cs = "__TEST_6__"
    common = dict(
        call_sign=cs, vessel_name=None, stage=AssessmentStage.BEFORE_ARRIVAL, wharf_name="효성부두",
        level=AssessmentLevel.UNFIT, headline="테스트",
        action=None,
    )
    detail = lambda names: {"alternatives": {"suggested": [{"rank": i + 1, "wharf_name": n} for i, n in enumerate(names)]}}  # noqa: E731
    assert await record_assessment(db, **common, action_detail=detail(["A"]), suggested_key="A")
    await db.flush()
    assert not await record_assessment(db, **common, action_detail=detail(["A"]), suggested_key="A")
    assert await record_assessment(db, **common, action_detail=detail(["B"]), suggested_key="B")
