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
        packaging_violations=[], unassessed_pairs=[],
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
        target_row=SimpleNamespace(name_ko="벤젠", chem_id="x", cas_no=None, packing_group="Ⅱ"), conflicts=[],
        bulk_compatibility_conflicts=[], packaging_violations=[], unassessed_pairs=[],
        rule_engine_floor=RiskLevel.SAFE, unload_method_name=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_facts_no_neighbors_and_no_packaging_check():
    # [2026-09-29] 하역방식 미신고 문장은 용기등급 Ⅰ 화물에만 — 나머지는 하역방식이 판정을 바꾸지 않는다
    s = _checked_facts([_verdict()], 0, [], RiskLevel.SAFE)
    assert s.startswith("[혼재 판정 안전")
    assert "혼재 검사 대상이 없었습니다" in s and "하역방식" not in s
    grade1 = SimpleNamespace(name_ko="아크릴로니트릴", chem_id="y", cas_no=None, packing_group="Ⅰ")
    assert "하역방식이 신고되지 않아" in _checked_facts([_verdict(target_row=grade1)], 0, [], RiskLevel.SAFE)


def test_facts_neighbors_clear():
    s = _checked_facts([_verdict(unload_method_name="펌프")], 4, [], RiskLevel.SAFE)
    assert "이웃 화물 4건과" in s and "충돌이 없습니다" in s and "부적합은 없습니다" in s


# ── 여러 화물: 모두 안전이면 종합, 아니면 문제 화물만 (2026-09-29) ──
from app.agents.safety.service import _focus  # noqa: E402


def _named(name, level=RiskLevel.SAFE, **kw):
    row = SimpleNamespace(name_ko=name, chem_id=name, cas_no=None, packing_group="Ⅱ")
    return _verdict(target_row=row, rule_engine_floor=level, unload_method_name="펌프", **kw)


def test_all_safe_cargos_are_described_together():
    vs = [_named("염화비닐"), _named("아이소부텐")]
    focus = _focus(vs, [])
    assert [x.target_row.name_ko for x in focus] == ["염화비닐", "아이소부텐"]
    s = _checked_facts(focus, 8, [], RiskLevel.SAFE)
    assert s.startswith("[혼재 판정 안전 · 화물 2종 종합]") and "염화비닐" not in s


def test_only_risky_cargo_is_described():
    vs = [_named("케로젠"), _named("러버 솔벤트", RiskLevel.CAUTION), _named("가솔린")]
    focus = _focus(vs, [])
    assert [x.target_row.name_ko for x in focus] == ["러버 솔벤트"]
    assert _checked_facts(focus, 8, [], RiskLevel.CAUTION).startswith("[혼재 판정 주의]\n")


def test_onboard_conflict_names_the_pair():
    onboard = [SimpleNamespace(cargo_a_name="황산", cargo_b_name="가솔린")]
    vs = [_named("케로젠"), _named("황산"), _named("가솔린")]
    assert {x.target_row.name_ko for x in _focus(vs, onboard)} == {"황산", "가솔린"}


# ── 이웃 화물 이름 · 머리 뒤 줄바꿈 · GHS 주요 위험성 (2026-09-29) ──
from app.agents.safety.service import _hazard_lines  # noqa: E402


def test_neighbor_names_and_header_newline():
    s = _checked_facts([_verdict(adjacent_names=["가솔린", "케로젠"], unload_method_name="펌프")], 5, [], RiskLevel.SAFE)
    head, body = s.split("\n", 1)
    assert head == "[혼재 판정 안전]"
    assert body.startswith("이웃 화물 5건(가솔린, 케로젠)과")


def _ghs(name, classes):
    payload = {"detail02": {"data": [{"msdsItemCode": "B02", "itemDetail": "|".join(f"{k} : {g}" for k, g in classes)}]}}
    return SimpleNamespace(target_row=SimpleNamespace(name_ko=name, chem_id=name, cas_no=None, packing_group="Ⅲ",
                                                      msds_payload=payload))


def test_hazards_follow_ghs_per_cargo():
    # 실측 오류 재현: 디젤·케로젠(구분3)을 '고인화성'으로 묶고, 없는 '눈 자극'을 쓰던 문제
    focus = [
        _ghs("러버 솔벤트", [("인화성 액체", "구분1"), ("발암성", "구분1B"), ("흡인 유해성", "구분1")]),
        _ghs("가솔린", [("인화성 액체", "구분2"), ("발암성", "구분1B"), ("흡인 유해성", "구분1")]),
        _ghs("케로젠", [("인화성 액체", "구분3"), ("발암성", "구분2"), ("흡인 유해성", "구분1")]),
        _ghs("디젤 연료", [("인화성 액체", "구분3"), ("발암성", "구분2"), ("피부 부식성/피부 자극성", "구분2")]),
    ]
    lines = _hazard_lines(focus)
    assert lines[0] == "인화성 액체: 러버 솔벤트(극인화성) · 가솔린(고인화성) · 케로젠·디젤 연료(인화성)"
    assert "흡인 유해성: 러버 솔벤트·가솔린·케로젠(1)" in lines
    assert "발암성: 러버 솔벤트·가솔린(1B) · 케로젠·디젤 연료(2, 의심)" in lines
    assert not any("눈" in l for l in lines)


def test_single_cargo_hazards_have_no_name():
    assert _hazard_lines([_ghs("가솔린", [("인화성 액체", "구분2")])]) == ["인화성 액체 고인화성"]


def test_hazard_order_and_inhalation_parse():
    # 센 구분이 앞 · 분류명 안 콜론('흡입: 증기')에서 자르지 않는다
    from app.agents.safety.msds_context import ghs_classification
    payload = {"detail02": {"data": [{"msdsItemCode": "B02", "itemDetail": "급성 독성(흡입: 증기) : 구분4"}]}}
    assert ghs_classification(payload) == [("급성 독성(흡입: 증기)", "구분4")]
    lines = _hazard_lines([_ghs("디젤 연료", [("인화성 액체", "구분3")]), _ghs("가솔린", [("인화성 액체", "구분2")])])
    assert lines == ["인화성 액체: 가솔린(고인화성) · 디젤 연료(인화성)"]
