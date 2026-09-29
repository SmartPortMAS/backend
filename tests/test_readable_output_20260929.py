"""관제사용 반환 정리 (2026-09-29) — 예보 공백, 안전 판정 근거·확인 필요, 기상 참고값."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agents import tools
from app.agents.safety.prompt import build_user_prompt
from app.agents.safety.schemas import RiskLevel
from app.agents.safety.service import _needs_check, _pick_governing, _verdict_basis
from app.agents.weather import service as weather_service
from app.agents.weather.schemas import ObservationFactor, WeatherAssessmentResult, WorkStatus

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
_THRESHOLD = SimpleNamespace(
    stop_wind_ms=14.0, unberth_wind_ms=17.0, disconnect_wind_ms=21.0,
    stop_wave_m=None, unberth_wave_m=None, disconnect_wave_m=None,
)


async def _warning(monkeypatch, rows, hours):
    async def fake_range(db, *, start, end):
        return rows

    monkeypatch.setattr(weather_service, "get_forecast_range", fake_range)
    return await weather_service._build_forecast_warning(
        None, as_of=NOW, expected_completion_at=NOW + timedelta(hours=hours),
        threshold=_THRESHOLD, extra_condition_active=False,
    )


def _row(h, wind):
    return SimpleNamespace(fcst_at_utc=NOW + timedelta(hours=h), wind_speed_ms=wind, wave_height_m=None, precip_mm=0.0)


@pytest.mark.asyncio
async def test_null_forecast_is_gap_not_deterioration(monkeypatch):
    fw = await _warning(monkeypatch, [_row(1, 3.0), _row(2, 3.0), _row(3, None), _row(4, None)], 4)
    assert fw.will_deteriorate is False and fw.worst_status is WorkStatus.NORMAL
    assert fw.no_forecast_after_utc == NOW + timedelta(hours=3)


@pytest.mark.asyncio
async def test_forecast_ending_early_is_gap(monkeypatch):
    fw = await _warning(monkeypatch, [_row(1, 3.0), _row(2, 3.0)], 48)
    assert fw.no_forecast_after_utc == NOW + timedelta(hours=2)


@pytest.mark.asyncio
async def test_forecast_wave_not_applied_to_inner_berth(monkeypatch):
    # 9/29 우선호@달포부두 재현 — 풍속은 멀쩡한데 예보 파고로 "13:00 부터 '하역중단' 예보" 조건이 붙었다.
    # 현재 관측과 같이, 항내 부두에는 외해 파고(예보 포함)를 쓰지 않는다.
    threshold = SimpleNamespace(**{**vars(_THRESHOLD), "stop_wave_m": 1.5})
    rows = [SimpleNamespace(fcst_at_utc=NOW + timedelta(hours=1), wind_speed_ms=3.0, wave_height_m=2.0, precip_mm=0.0)]

    async def fake_range(db, *, start, end):
        return rows

    monkeypatch.setattr(weather_service, "get_forecast_range", fake_range)
    kw = dict(as_of=NOW, expected_completion_at=NOW + timedelta(hours=1), threshold=threshold,
              extra_condition_active=False)
    inner = await weather_service._build_forecast_warning(None, **kw, wave_applies=False)
    buoy = await weather_service._build_forecast_warning(None, **kw, wave_applies=True)
    assert inner.will_deteriorate is False
    assert buoy.will_deteriorate is True
    # 무엇이 기준을 넘는 예보인지 함께 싣는다 — "하역중단 예보"만으로는 원인을 몰랐다
    assert buoy.earliest_deterioration_causes == ["파고 2.0m >= 1.5m"]


@pytest.mark.asyncio
async def test_real_deterioration_still_counts(monkeypatch):
    fw = await _warning(monkeypatch, [_row(1, 3.0), _row(2, 15.0)], 2)
    assert fw.will_deteriorate is True and fw.no_forecast_after_utc is None


def _msds(name="케로젠", packing_group="Ⅲ", flash=None):
    return SimpleNamespace(name_ko=name, name_en=None, chem_id=name,
                           packing_group=packing_group, flash_point_celsius=flash)


def _verdict(**kw):
    base = dict(conflicts=[], bulk_compatibility_conflicts=[], packaging_violations=[],
                unassessed_pairs=[], unload_method_name=None, target_row=_msds(),
                rule_engine_floor=RiskLevel.SAFE)
    base.update(kw)
    return SimpleNamespace(**base)


def test_basis_puts_onboard_conflict_first():
    onboard = [SimpleNamespace(cargo_a_name="에틸렌디아민", cargo_b_name="아크릴산")]
    lines = _verdict_basis([_verdict()], 4, onboard)
    assert lines[0].startswith("같은 선박 화물끼리 혼재 충돌 1쌍(에틸렌디아민↔아크릴산)")
    assert lines[-1] == "이웃 화물 4건과 MSDS·46 CFR 150 기준 충돌 없음"


def test_basis_no_neighbors_is_stated():
    assert _verdict_basis([_verdict()], 0, []) == ["이웃 선석에 실린 화물 없음 — 혼재 비교 대상 없음"]


def test_needs_check_packaging_and_unassessed():
    pair = SimpleNamespace(adjacent_name="황산", adjacent_chem_id="1", reason="MSDS 근거 없음")
    grade1 = _msds(packing_group="Ⅰ")
    assert _needs_check([_verdict(unassessed_pairs=[pair], target_row=grade1)]) == [
        "이웃 화물 황산 — 혼재 판정 근거 부족(MSDS 근거 없음)",
        "하역방식 — 신고가 없어 포장·하역 적합성은 보지 못했습니다",
    ]
    assert _needs_check([_verdict(unload_method_name="펌프", target_row=grade1)]) == []


@pytest.mark.parametrize("packing_group", ["Ⅱ", "Ⅲ", None])
def test_needs_check_skips_unload_method_when_it_cannot_change_verdict(packing_group):
    # 하역방식은 용기등급 Ⅰ 화물만 판정한다(rule_engine.find_packing_violation) — 나머지는 몰라도 된다
    assert _needs_check([_verdict(target_row=_msds(packing_group=packing_group))]) == []


def _nitric(target_group="33"):
    return (
        SimpleNamespace(adjacent_berth="옆 부두", adjacent_name="질산", adjacent_chem_id="001052",
                        shared_category="가연성물질"),
        SimpleNamespace(adjacent_berth="옆 부두", adjacent_name="질산", adjacent_chem_id="001052",
                        target_group=target_group, target_group_name="Misc. Hydrocarbon Mixtures",
                        adjacent_group="3", adjacent_group_name="Nitric Acids"),
    )


def test_basis_merges_same_conflict_across_cargos_with_names():
    # 가솔린·케로젠이 둘 다 질산과 충돌 — 예전엔 대표(가솔린) 것만, 우리 화물 이름 없이 나왔다
    verdicts = []
    for name in ("가솔린", "케로젠"):
        msds, cfr = _nitric()
        verdicts.append(_verdict(target_row=_msds(name), conflicts=[msds], bulk_compatibility_conflicts=[cfr]))
    assert _verdict_basis(verdicts, 1, []) == [
        "이웃 화물 충돌: 가솔린·케로젠 ↔ 옆 부두 질산 — MSDS '가연성물질' 혼재금지 / 46 CFR 150 "
        "Misc. Hydrocarbon Mixtures(33) ↔ Nitric Acids(3)",
    ]


def test_basis_one_line_per_adjacent_cargo():
    # 4부두 1·2선석의 질산 하나가 MSDS·46 CFR 150 두 기준으로 4줄이 되던 것(사용자 지적 9/29)을 한 줄로
    lines = []
    for berth in ("4부두-1선석", "4부두-2선석"):
        msds, cfr = _nitric(target_group="30")
        msds.adjacent_berth = cfr.adjacent_berth = berth
        cfr.target_group_name = "Olefins"
        lines.append((msds, cfr))
    v = _verdict(conflicts=[m for m, _ in lines], bulk_compatibility_conflicts=[c for _, c in lines])
    assert _verdict_basis([v], 2, []) == [
        "이웃 화물 충돌: 4부두 1·2선석 질산 — MSDS '가연성물질' 혼재금지 / 46 CFR 150 Olefins(30) ↔ Nitric Acids(3)",
    ]


def test_basis_single_cargo_keeps_old_wording():
    msds, _ = _nitric()
    assert _verdict_basis([_verdict(conflicts=[msds])], 1, []) == [
        "이웃 화물 충돌: 옆 부두 질산 — MSDS '가연성물질' 혼재금지",
    ]


def test_needs_check_covers_non_governing_cargos():
    # 대표가 아닌 화물의 근거 부족·하역방식 누락도 빠지지 않는다 — 어느 화물인지 이름을 붙인다
    pair = SimpleNamespace(adjacent_name="황산", adjacent_chem_id="1", reason="MSDS 근거 없음")
    verdicts = [
        _verdict(target_row=_msds("가솔린", "Ⅱ")),
        _verdict(target_row=_msds("디젤 연료"), unassessed_pairs=[pair]),
        _verdict(target_row=_msds("러버 솔벤트", "Ⅰ")),
    ]
    assert _needs_check(verdicts) == [
        "디젤 연료 · 이웃 화물 황산 — 혼재 판정 근거 부족(MSDS 근거 없음)",
        "러버 솔벤트 · 하역방식 — 신고가 없어 포장·하역 적합성은 보지 못했습니다",
    ]


def test_safety_prompt_checks_every_cargo_conflict():
    # 대표 화물 것만 넘기면 다른 화물의 충돌이 체크리스트에서 빠졌다
    lines = ["이웃 화물 충돌: 가솔린·케로젠 ↔ 옆 부두 질산 — MSDS '가연성물질' 혼재금지"]
    p = build_user_prompt(
        target_cargo_name="가솔린", hazard_summary={}, conflicts=[], bulk_compatibility_conflicts=[],
        packaging_violations=[], unassessed_pairs=[], adjacent_count=1, unload_method_name="펌프",
        conflict_checks=lines,
    )
    assert "  - 이웃 화물 충돌: 가솔린·케로젠 ↔ 옆 부두 질산" in p


def test_governing_tie_prefers_packing_group_then_flash_point():
    # 붙여 준 6종 화물 — 모두 '안전'이면 예전엔 목록 맨 앞(케로젠)이 대표였다
    verdicts = [_verdict(target_row=_msds(n, pg, fp)) for n, pg, fp in (
        ("케로젠", "Ⅲ", 29.0), ("디젤 연료", "Ⅲ", 58.0), ("가솔린", "Ⅱ", -40.0),
        ("러버 솔벤트", "Ⅰ", None), ("스토다드 솔벤트", "Ⅱ", 21.0), ("아스팔트", "Ⅱ", 37.78),
    )]
    assert _pick_governing(verdicts).target_row.name_ko == "러버 솔벤트"
    assert _pick_governing(verdicts[:3]).target_row.name_ko == "가솔린"            # Ⅱ > Ⅲ
    assert _pick_governing([verdicts[4], verdicts[2]]).target_row.name_ko == "가솔린"  # 같은 Ⅱ → 인화점 낮은 쪽


def test_governing_higher_risk_beats_packing_group():
    low = _verdict(target_row=_msds("러버 솔벤트", "Ⅰ"))
    high = _verdict(target_row=_msds("케로젠", "Ⅲ", 29.0), rule_engine_floor=RiskLevel.DANGER)
    assert _pick_governing([low, high]) is high


def test_governing_full_tie_keeps_request_order():
    a, b = _verdict(target_row=_msds("A", "Ⅱ", 10.0)), _verdict(target_row=_msds("B", "Ⅱ", 10.0))
    assert _pick_governing([a, b]) is a


def test_unassessed_pairs_fold_into_one_line_with_common_reason():
    # SH SOPHIA 재현 — 이웃 30건마다 대상 화물 쪽 자료 공백 4구절이 되풀이됐다
    common = "'X'은 46 CFR Part 150 호환성 그룹 미등재 / 'X'의 MSDS에 화물 대상 기피 정보 없음"
    pairs = [SimpleNamespace(adjacent_name=n, adjacent_chem_id=n, reason=f"{common} / '{n}'의 MSDS에 기피 정보 없음")
             for n in ("케로젠", "디젤", "가솔린", "나프타")]
    v = _verdict(unassessed_pairs=pairs, unload_method_name="펌프")
    assert _needs_check([v]) == [
        "이웃 화물 케로젠, 디젤, 가솔린 외 1종 — 혼재 판정 근거 부족"
        "('X'은 46 CFR Part 150 호환성 그룹 미등재; 'X'의 MSDS에 화물 대상 기피 정보 없음)"
    ]
    basis = _verdict_basis([v], 30, [])
    assert basis[0].startswith("이웃 화물 4종은 혼재 판정 근거가 없음")
    assert basis[-1] == "판정 근거가 있는 이웃 화물과는 MSDS·46 CFR 150 기준 충돌 없음"


def test_safety_prompt_has_no_grade_and_lists_what_to_check():
    onboard = [SimpleNamespace(cargo_a_name="A", cargo_b_name="B", basis="46 CFR 150", detail="아민↔아크릴레이트")]
    p = build_user_prompt(
        target_cargo_name="큐멘", hazard_summary={}, conflicts=[], bulk_compatibility_conflicts=[],
        packaging_violations=[], unassessed_pairs=[], adjacent_count=4, unload_method_name=None,
        onboard_conflicts=onboard, needs_check=["하역방식 — 신고가 없어 포장·하역 적합성은 보지 못했습니다"],
    )
    # 등급을 주면 LLM 이 그 원인을 유해성에서 찾았다 — 등급 자체를 넘기지 않는다
    assert "확정 위험등급" not in p
    assert "A ↔ B 같은 선박 적재 — 격리 적재 확인" in p and "하역방식 — 신고가 없어" in p


def test_weather_reference_value_is_not_evidence():
    obs = ObservationFactor(value=1.2, unit="m/s", is_stale=False)
    w = WeatherAssessmentResult(
        status=WorkStatus.NORMAL, assessed_at_utc=NOW, wind=obs, wave=obs,
        reasons=["풍속 1.2m/s < 14.0m/s(중단 임계) - 정상",
                 "파고 0.3m는 외해 부이(22189) 관측이라 이 항내 계선시설 판정에 적용하지 않음 - 참고값"],
    )
    o = tools.weather_opinion(w)
    assert o.evidence == ["풍속 1.2m/s < 14.0m/s(중단 임계) - 정상"]
    # 판정에 안 쓴 파고는 '확인한 것'이 아니라 참고(notes)다 — 칩에 있으면 확인한 근거처럼 읽혔다
    assert o.notes and "적용하지 않음" in o.notes[0]
    assert not any("파고" in c for c in o.checked)
