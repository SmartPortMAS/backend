"""혼재 판정 회귀 평가셋 — 점검(25번 문서)에서 찾은 사례를 고정한다 (2026-09-27).

로컬 개발 DB·Neo4j 그래프를 그대로 읽는다(LLM 없음, 규칙 하한만 본다).
그래프를 다시 적재하거나 규칙을 바꿀 때 이 파일이 기준이다.

두 종류를 함께 둔다.
  · 오판 사례 — MSDS 상투 문구('가연성 물질' 기피)가 만든 가짜 충돌. MSDS 충돌 0건이어야 한다.
  · 지켜야 할 충돌 — 노이즈를 걷어내다 진짜 위험까지 지우지 않았는지 확인한다.
"""
import pytest

from app.agents.safety.schemas import AdjacentCargo, CargoRef, RiskLevel, SafetyAssessmentRequest
from app.agents.safety.service import assess_verdict
from sqlalchemy import text

_ORDER = [RiskLevel.SAFE, RiskLevel.CAUTION, RiskLevel.DANGER, RiskLevel.BLOCKED]

# (대상, 인접, 최소 등급, 최대 등급, MSDS 충돌이 있어야 하나)
CASES = [
    # 25번 문서 Q2 완료 기준
    ("황산", "수산화나트륨", RiskLevel.BLOCKED, RiskLevel.BLOCKED, None),   # 벌크 그룹 2↔5
    ("질산", "톨루엔", RiskLevel.BLOCKED, RiskLevel.BLOCKED, None),
    ("톨루엔", "벤젠", RiskLevel.SAFE, RiskLevel.CAUTION, False),
    ("에틸렌 글리콜", "가솔린", RiskLevel.SAFE, RiskLevel.CAUTION, False),
    ("1-DODECENE", "2-에틸 헥산올", RiskLevel.SAFE, RiskLevel.CAUTION, False),
    # 점검 때 본 오판: 연료유↔톨루엔 '위험'
    ("연료, 잔사유(FUEL OIL, RESIDUAL)", "톨루엔", RiskLevel.SAFE, RiskLevel.CAUTION, False),
    # 지켜야 할 충돌
    ("황산", "가솔린", RiskLevel.DANGER, RiskLevel.BLOCKED, True),         # 진한 황산은 산화성
    ("프로필렌", "황산", RiskLevel.BLOCKED, RiskLevel.BLOCKED, None),     # 벌크 그룹 30↔2
]


async def _chem_id(db, name: str) -> str:
    chem = (await db.execute(
        text("SELECT chem_id FROM msds_chemical WHERE name_ko = :n"), {"n": name},
    )).scalar()
    assert chem, f"평가셋 물질이 msds_chemical 에 없다: {name}"
    return chem


@pytest.mark.asyncio
@pytest.mark.parametrize("target,adjacent,lo,hi,msds_conflict", CASES,
                         ids=[f"{c[0]}-{c[1]}" for c in CASES])
async def test_safety_pair(db, neo4j, target, adjacent, lo, hi, msds_conflict):
    request = SafetyAssessmentRequest(
        target_cargo=CargoRef(chem_id=await _chem_id(db, target)),
        adjacent_cargos=[AdjacentCargo(
            berth_name="평가", cargo=CargoRef(chem_id=await _chem_id(db, adjacent)), distance_m=None,
        )],
    )
    verdict = await assess_verdict(db, neo4j, request)
    floor = verdict.rule_engine_floor
    assert _ORDER.index(lo) <= _ORDER.index(floor) <= _ORDER.index(hi), (
        f"{target}↔{adjacent}: 규칙 하한 {floor.value} (기대 {lo.value}~{hi.value}) "
        f"MSDS={[c.shared_category for c in verdict.conflicts]} "
        f"벌크={[c.reason for c in verdict.bulk_compatibility_conflicts]}"
    )
    if msds_conflict is not None:
        assert bool(verdict.conflicts) is msds_conflict, (
            f"{target}↔{adjacent}: MSDS 충돌 {[c.shared_category for c in verdict.conflicts]}"
        )


# ── 벌크 그룹 판정 근거 (2026-09-27) ─────────────────────────────────────────
# 양쪽 46 CFR Part 150 그룹이 확인되면 그 쌍은 '판정함'이다 — 미평가 쌍이 없어야 한다.
# 예전에는 MSDS '피해야 할 물질'이 비어 크실렌·암모니아가 낀 쌍이 전부 '확인요청'이었다.
# (대상, 인접, 벌크 충돌이 있어야 하나)
BULK_CASES = [
    ("크실렌", "케로젠", False),                 # 32 ↔ 33 — 화물그룹끼리는 반응하지 않는다
    ("암모니아", "가솔린", False),               # 6 ↔ 33 — 차트상 호환
    # Appendix I(b) — 차트상 호환이지만 원문이 금지한 조합 (9/27 전에는 통과했다)
    ("아크릴로니트릴", "수산화나트륨", True),
    ("메틸 아크릴로니트릴", "수산화 칼륨", True),
    ("트리클로로에틸렌", "수산화나트륨", True),
    ("이염화에틸렌", "1,2-디아미노에탄", True),
    ("메틸삼차 부틸에테르", "인산", True),
    # Appendix I(a) — 차트상 비호환(5↔20)이지만 원문이 시험으로 풀어 준 조합 (9/27 전에는 막았다)
    ("수산화나트륨", "메틸 알코올", False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("target,adjacent,bulk_conflict", BULK_CASES,
                         ids=[f"{c[0]}-{c[1]}" for c in BULK_CASES])
async def test_bulk_group_basis(db, neo4j, target, adjacent, bulk_conflict):
    request = SafetyAssessmentRequest(
        target_cargo=CargoRef(chem_id=await _chem_id(db, target)),
        adjacent_cargos=[AdjacentCargo(
            berth_name="평가", cargo=CargoRef(chem_id=await _chem_id(db, adjacent)), distance_m=None,
        )],
    )
    verdict = await assess_verdict(db, neo4j, request)
    assert not verdict.unassessed_pairs, (
        f"{target}↔{adjacent}: 양쪽 벌크 그룹이 있는데 미평가 {[p.reason for p in verdict.unassessed_pairs]}"
    )
    assert bool(verdict.bulk_compatibility_conflicts) is bulk_conflict, (
        f"{target}↔{adjacent}: 벌크 {[c.reason for c in verdict.bulk_compatibility_conflicts]}"
    )
    if bulk_conflict:
        assert verdict.rule_engine_floor is RiskLevel.BLOCKED
