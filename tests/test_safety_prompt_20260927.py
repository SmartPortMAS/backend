"""안전 LLM 입력이 '하지 않은 검사'를 사실대로 적는가 (2026-09-27).

실측 사례: 이웃 0건·하역방식 미신고로 부른 판정에서 LLM 이 "인접 선석 충돌 없음으로
확인", "포장·하역방식 검증이 이루어졌으며"라고 썼다. 입력이 "충돌 없음"·"판정 근거가
확보됨"·"부적합 없음 — 또는 판정 안 함"으로 모호했기 때문이다.
"""
from app.agents.safety.prompt import build_user_prompt
from app.agents.safety.schemas import RiskLevel


def _prompt(**kw):
    base = dict(
        target_cargo_name="벤젠", hazard_summary={}, conflicts=[], bulk_compatibility_conflicts=[],
        packaging_violations=[], unassessed_pairs=[], rule_engine_floor=RiskLevel.SAFE,
        adjacent_count=0, unload_method_name=None,
    )
    base.update(kw)
    return build_user_prompt(**base)


def test_no_neighbors_is_stated_not_as_clear():
    p = _prompt(adjacent_count=0)
    assert "인접 화물 없음" in p
    assert "판정 근거가 확보됨" not in p
    assert "충돌 없음)" not in p


def test_neighbors_present_keeps_normal_wording():
    p = _prompt(adjacent_count=3)
    assert "인접 화물 없음" not in p
    assert "판정 근거가 확보됨" in p


def test_packaging_not_checked_without_unload_method():
    p = _prompt(unload_method_name=None)
    assert "검사하지 않음" in p


def test_packaging_checked_with_unload_method():
    p = _prompt(unload_method_name="펌프")
    assert "검사하지 않음" not in p and "'펌프' 기준" in p


# ── 코드가 확정하는 검사 사실 문장 ──
from types import SimpleNamespace  # noqa: E402

from app.agents.safety.service import _checked_facts  # noqa: E402


def _verdict(**kw):
    base = dict(
        target_row=SimpleNamespace(name_ko="벤젠", chem_id="x", cas_no=None), conflicts=[],
        bulk_compatibility_conflicts=[], packaging_violations=[], unassessed_pairs=[],
        rule_engine_floor=RiskLevel.SAFE, unload_method_name=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_facts_no_neighbors_and_no_packaging_check():
    s = _checked_facts(_verdict(), 0)
    assert s.startswith("[혼재 판정 안전")
    assert "혼재 검사 대상이 없었습니다" in s and "검사하지 않았습니다" in s


def test_facts_neighbors_clear():
    s = _checked_facts(_verdict(unload_method_name="펌프"), 4)
    assert "인접 화물 4건과" in s and "충돌이 없습니다" in s and "부적합은 없습니다" in s
