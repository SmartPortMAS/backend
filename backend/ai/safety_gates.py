"""
안전 게이트 R1~R15 + risk_level 결정론화 (P5+P4)

온산항_MVP범위_안전규칙시드.md 의 R1~R14 + 벤젠 5% 특별취급(R15)을 결정론 게이트로
구현한다. 규칙 1개 = 함수 1개 = 위험유형 1개이며, 신규 입항 시 전부 실행해 빠짐없이
탐지한다. 같은 입력이면 같은 결과가 나온다 (난수/LLM 판단 없음).

risk_level 은 GAP5 권고대로 (rule_engine_floor, IMDG 격리코드, 인화성등급, 게이트 히트)의
결정론 함수로 확정한다. LLM 은 checklist/reasoning/summary 문장 생성에만 쓴다
(compose_explanation 자리 - 현재는 결정론 템플릿, LLM 연결 시에도 risk_level 은 불변).

데이터 소스 (onsan_mvp/data 재사용):
  - berth_restrictions.csv: R3/R4/R5 서브선석별 흘수/DWT/전장
  - cargo_cas_map.csv: R13/R14 인화점, IMDG class
  - onsan_adjacency_edges.csv: R13 인접 거리 (IMDG 격리거리 3/6/12/24m 와 비교)
"""
import csv
import logging
import os
import re
from typing import Optional

from ai.graph_rag import evaluate_safety
from simulator.data_generator import BERTH_LIST, CARGO_MSDS_DB

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ONSAN_DATA = os.path.join(_REPO_ROOT, "onsan_mvp", "data")

_NAME2ID = {b["name"]: b["berth_id"] for b in BERTH_LIST}

# 위험등급 오름차순. risk_level 은 이 축 위의 max 로만 결정된다.
RISK_ORDER = ["안전", "주의", "위험", "배정불가"]
_SEVERITY_TO_RISK = {
    "WARNING": "주의", "LIMIT": "주의",
    "HOLD": "위험",
    "HARD_BLOCK": "배정불가", "EMERGENCY": "배정불가",
}

# IMDG 격리코드 -> 요구 이격거리(m). 기존 시스템 매핑(IMDG Code Ch 7.2) 재사용.
SEG_DISTANCE_M = {1: 3, 2: 6, 3: 12, 4: 24}

LOW_FLASHPOINT_C = 23.0  # 저인화점 임계 (위험물 분류 기준)
DAY_START, DAY_END = 6, 18  # 주간 작업시간대


# ─── 데이터 로드 (모듈 로드 시 1회, 결정론) ───

def _load_cargo_map() -> dict:
    table = {}
    with open(os.path.join(_ONSAN_DATA, "cargo_cas_map.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            table[r["cargo_name"].strip()] = {
                "un_no": (r.get("un_no") or "").strip(),
                "imdg_class": (r.get("imdg_class") or "").strip(),
                "flashpoint_raw": (r.get("flashpoint_c_ref") or "").strip(),
                "category": (r.get("cargo_category") or "").strip(),
            }
    return table


def _load_restrictions() -> list:
    def num(v):
        v = (v or "").strip()
        try:
            return float(v) if v else None
        except ValueError:
            return None
    rows = []
    with open(os.path.join(_ONSAN_DATA, "berth_restrictions.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append({
                "berth_ref": (r.get("berth_ref") or "").strip(),
                "sub_berth": (r.get("sub_berth") or "").strip(),
                "max_draught_m": num(r.get("최대흘수_m")),
                "max_dwt": num(r.get("접안_DWT")),
                "max_loa_m": num(r.get("최대전장_m")) or num(r.get("전장_m")),
                "min_loa_m": num(r.get("최소전장_m")),
            })
    return rows


def _load_adjacency() -> dict:
    """{frozenset({berth_id_a, berth_id_b}): distance_m}"""
    table = {}
    with open(os.path.join(_ONSAN_DATA, "onsan_adjacency_edges.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            table[frozenset({r["a"].strip(), r["b"].strip()})] = float(r["distance_m"])
    return table


_CARGO_MAP = _load_cargo_map()
_RESTRICTIONS = _load_restrictions()
_ADJACENCY = _load_adjacency()


def _restrictions_for(berth_id: Optional[str]) -> list:
    """S-Oil 은 berth_ref 가 'SA-SO'/'WS-SO' 프리픽스라 startswith 로 매칭."""
    if not berth_id:
        return []
    return [r for r in _RESTRICTIONS
            if r["berth_ref"] == berth_id or berth_id.startswith(r["berth_ref"])]


def parse_flashpoint(raw: str) -> Optional[float]:
    """'-11', '>55', '<-18', '38-72' -> 대표값(첫 숫자). 'gas'/'N/A' -> None."""
    raw = (raw or "").strip()
    if not raw or raw.lower() in {"n/a", "na", "gas"}:
        return None
    m = re.search(r"-?\d+(\.\d+)?", raw)
    return float(m.group()) if m else None


def flammability_grade(cargo_name: str) -> str:
    info = _CARGO_MAP.get(cargo_name)
    if info is None:
        return "미등록"
    if info["imdg_class"] == "2.1":
        return "인화성가스"
    fp = parse_flashpoint(info["flashpoint_raw"])
    if fp is None:
        return "비인화성/미상"
    if fp < LOW_FLASHPOINT_C:
        return "저인화점"
    if fp <= 60:
        return "인화성"
    return "고인화점"


def segregation_code(class_a: str, class_b: str) -> int:
    """IMDG class 쌍 -> 격리코드 1~4 (MVP 근사, IMDG Ch 7.2 컨벤션)."""
    pair = {class_a, class_b}
    if pair == {"2.1"}:
        return 4
    if "2.1" in pair:
        return 3
    if pair == {"3"}:
        return 2
    return 1


def _adjacent_distance(berth_a: str, berth_b: str) -> Optional[float]:
    ida, idb = _NAME2ID.get(berth_a), _NAME2ID.get(berth_b)
    if not ida or not idb:
        return None
    return _ADJACENCY.get(frozenset({ida, idb}))


def adjacent_berth_names(berth_name: str) -> set:
    """ADJACENT_TO 엣지가 있는 선석명 집합 (오케스트레이터 인접작업 수집용)."""
    bid = _NAME2ID.get(berth_name)
    if not bid:
        return set()
    id2name = {v: k for k, v in _NAME2ID.items()}
    out = set()
    for pair in _ADJACENCY:
        if bid in pair:
            other = next(iter(pair - {bid}), None)
            if other and other in id2name:
                out.add(id2name[other])
    return out


# ─── 게이트 R1~R15 (함수 1개 = 규칙 1개, hit/severity/reason 반환) ───

def _gate(rule, name, hit, severity, reason):
    return {"rule": rule, "name": name, "hit": bool(hit),
            "severity": severity if hit else None, "reason": reason}


def r1_sire(ctx):
    applies = ctx["cargo_category"] in {"유류", "원유"}
    if not applies:
        return _gate("R1", "유조선 SIRE 승인", False, None, "해당없음(유조선 아님)")
    ok = ctx.get("sire_valid") is True
    return _gate("R1", "유조선 SIRE 승인", not ok, "HARD_BLOCK",
                 "SIRE 승인 이력 확인" if ok else "SIRE 승인 이력 없음 -> 접안 불가")


def r2_cdi(ctx):
    applies = ctx["cargo_category"] in {"액체화학", "친환경연료"}
    if not applies:
        return _gate("R2", "케미칼/LPG선 CDI 검사", False, None, "해당없음(케미칼/LPG선 아님)")
    ok = ctx.get("cdi_valid") is True
    return _gate("R2", "케미칼/LPG선 CDI 검사", not ok, "HARD_BLOCK",
                 "CDI 검사 이력 확인" if ok else "CDI 검사 이력 없음 -> 접안 불가")


def _fit_sub_berths(ctx, key_limit, value, cmp_over=True):
    rows = _restrictions_for(ctx.get("berth_id"))
    if not rows or value is None:
        return None, rows
    fits = [r for r in rows
            if r[key_limit] is None or (value <= r[key_limit] if cmp_over else value >= r[key_limit])]
    return fits, rows


def r3_draught(ctx):
    fits, rows = _fit_sub_berths(ctx, "max_draught_m", ctx.get("draught_m"))
    if fits is None:
        return _gate("R3", "흘수 vs 선석 최대흘수", False, None, "판정 데이터 없음(제한 미등록 또는 흘수 미입력)")
    if fits:
        return _gate("R3", "흘수 vs 선석 최대흘수", False, None,
                     f"적합 서브선석 {[r['sub_berth'] for r in fits]}")
    return _gate("R3", "흘수 vs 선석 최대흘수", True, "HARD_BLOCK",
                 f"흘수 {ctx['draught_m']}m 가 모든 서브선석 최대흘수 초과 "
                 f"(최대 {max(r['max_draught_m'] for r in rows if r['max_draught_m'] is not None)}m)")


def r4_dwt(ctx):
    fits, rows = _fit_sub_berths(ctx, "max_dwt", ctx.get("dwt"))
    if fits is None:
        return _gate("R4", "DWT vs 접안능력", False, None, "판정 데이터 없음")
    if fits:
        return _gate("R4", "DWT vs 접안능력", False, None,
                     f"적합 서브선석 {[r['sub_berth'] for r in fits]}")
    return _gate("R4", "DWT vs 접안능력", True, "HARD_BLOCK",
                 f"DWT {ctx['dwt']:,.0f} 이 모든 서브선석 접안능력 초과")


def r5_loa(ctx):
    loa = ctx.get("loa_m")
    rows = _restrictions_for(ctx.get("berth_id"))
    if not rows or loa is None:
        return _gate("R5", "전장 min/max", False, None, "판정 데이터 없음")
    fits = [r for r in rows
            if (r["max_loa_m"] is None or loa <= r["max_loa_m"])
            and (r["min_loa_m"] is None or loa >= r["min_loa_m"])]
    if fits:
        return _gate("R5", "전장 min/max", False, None, f"적합 서브선석 {[r['sub_berth'] for r in fits]}")
    return _gate("R5", "전장 min/max", True, "HARD_BLOCK",
                 f"전장 {loa}m 가 모든 서브선석 전장 범위(min/max) 벗어남")


def r6_work_hours(ctx):
    draught, gt, hour = ctx.get("draught_m"), ctx.get("gt"), ctx.get("work_hour")
    if draught is None or gt is None:
        return _gate("R6", "작업시간대 제한", False, None, "판정 데이터 없음(흘수/GT 미입력)")
    exempt = draught < 9.0 and gt < 20000
    if exempt:
        return _gate("R6", "작업시간대 제한", False, None, "흘수 9m 미만 & 20,000GT 미만 -> 24시간 작업 가능")
    if hour is None:
        return _gate("R6", "작업시간대 제한", False, None, "주간(06~18시)만 작업 가능 조건 적용 (작업시각 미지정)")
    night = not (DAY_START <= hour < DAY_END)
    return _gate("R6", "작업시간대 제한", night, "LIMIT",
                 f"{hour}시 작업 요청 - 20,000GT 이상/흘수 9m 이상은 주간(06~18시)만 가능" if night
                 else "주간 작업시간대 내")


def r7_upa_approval(ctx):
    gt = ctx.get("gt")
    hit = gt is not None and gt >= 150000
    return _gate("R7", "150,000GT 이상 UPA 사전승인", hit, "WARNING",
                 f"GT {gt:,.0f} >= 150,000 -> UPA 사전승인 필요" if hit else "해당없음")


def r8_pilotage(ctx):
    gt = ctx.get("gt")
    if gt is None:
        return _gate("R8", "도선 의무", False, None, "판정 데이터 없음(GT 미입력)")
    threshold = 500 if ctx.get("international", True) else 2000
    required = gt >= threshold
    hit = required and ctx.get("pilot_onboard") is False
    return _gate("R8", "도선 의무", hit, "WARNING",
                 f"GT {gt:,.0f} >= {threshold}(도선 의무) 인데 도선사 미승선" if hit
                 else ("도선 의무 대상(승선 확인/미입력)" if required else "도선 의무 미대상"))


def r9_eta_report(ctx):
    h = ctx.get("eta_reported_hours_before")
    if h is None:
        return _gate("R9", "ETA 72/48시간 전 보고", False, None, "판정 데이터 없음(보고시점 미입력)")
    hit = h < 48
    return _gate("R9", "ETA 72/48시간 전 보고", hit, "WARNING",
                 f"ETA {h}시간 전 보고 - 48시간 미만, 절차 경고" if hit else f"ETA {h}시간 전 보고 완료")


def r10_ssscl(ctx):
    m = ctx.get("minutes_since_ssscl")
    if m is None:
        return _gate("R10", "Ship/Shore Safety Checklist 재확인", False, None, "해당없음(작업 전 또는 미입력)")
    hit = m > 120
    return _gate("R10", "Ship/Shore Safety Checklist 재확인", hit, "WARNING",
                 f"체크리스트 재확인 {m:.0f}분 경과 (>120분)" if hit else "재확인 주기 내")


_ANCHOR_RULES = {  # anchorage_id: (min_dwt, max_dwt)
    "E1": (None, 10000), "E2": (None, 30000), "E3": (20000, None),
    "W1": (None, 20000), "B1": (None, 10000), "B2": (None, 30000), "B3": (20000, None),
}


def r11_anchorage(ctx):
    anc, dwt = ctx.get("anchorage"), ctx.get("dwt")
    if not anc or anc not in _ANCHOR_RULES or dwt is None:
        return _gate("R11", "정박지 톤수 적합", False, None, "해당없음(정박지 미배정)")
    lo, hi = _ANCHOR_RULES[anc]
    hit = (lo is not None and dwt < lo) or (hi is not None and dwt > hi)
    return _gate("R11", "정박지 톤수 적합", hit, "WARNING",
                 f"DWT {dwt:,.0f} 이 {anc} 톤수 기준(min {lo}, max {hi}) 벗어남" if hit
                 else f"{anc} 톤수 기준 내")


def r12_emergency(ctx):
    ev = ctx.get("emergency") or {}
    active = [k for k, v in ev.items() if v]
    return _gate("R12", "화재/폭발/누출 비상", bool(active), "EMERGENCY",
                 f"비상 이벤트 발생: {active}" if active else "비상 이벤트 없음")


def r13_flammable_adjacency(ctx):
    grade = ctx["flammability_grade"]
    if grade not in {"저인화점", "인화성가스"}:
        return _gate("R13", "인화성 인접작업 격리", False, None, f"해당없음(화물 등급 {grade})")
    my_class = (_CARGO_MAP.get(ctx["cargo_name"]) or {}).get("imdg_class", "")
    for op in ctx.get("adjacent_operations") or []:
        other = _CARGO_MAP.get(op.get("cargo_name") or "")
        other_flammable = other and (other["imdg_class"] == "2.1"
                                     or (parse_flashpoint(other["flashpoint_raw"]) or 99) < LOW_FLASHPOINT_C)
        hot_work = op.get("activity") in {"hot_work", "벤팅"}
        if not (other_flammable or hot_work):
            continue
        dist = _adjacent_distance(ctx.get("berth_name", ""), op.get("berth_name", ""))
        if dist is None:
            continue  # 인접 엣지 없음(멀리 떨어짐)
        code = segregation_code(my_class, (other or {}).get("imdg_class", "3"))
        required = SEG_DISTANCE_M[code]
        if dist < required:
            return _gate("R13", "인화성 인접작업 격리", True, "HOLD",
                         f"인접 '{op['berth_name']}' {op.get('activity', '작업')} 중 "
                         f"'{op.get('cargo_name')}' - 거리 {dist}m < IMDG 격리코드 {code} 요구 {required}m "
                         f"-> 증기운 중첩 위험, 배정 보류")
    return _gate("R13", "인화성 인접작업 격리", False, None, "인접 격리거리 위반 없음")


def r14_lightning_heat(ctx):
    grade = ctx["flammability_grade"]
    if grade not in {"저인화점", "인화성가스"}:
        return _gate("R14", "낙뢰/고온 증기운 위험", False, None, f"해당없음(화물 등급 {grade})")
    w = ctx.get("weather") or {}
    lightning = bool(w.get("lightning"))
    hot = w.get("temp_c") is not None and w["temp_c"] >= 35.0
    hit = lightning or hot
    cause = "낙뢰 경보" if lightning else (f"고온 {w.get('temp_c')}°C" if hot else "")
    return _gate("R14", "낙뢰/고온 증기운 위험", hit, "WARNING",
                 f"{cause} + 저인화점 화물 하역 -> 증기운 위험 경보" if hit else "기상 발화 조건 없음")


def r15_benzene(ctx):
    pct = ctx.get("benzene_pct")
    if ctx["cargo_name"] == "벤젠" and pct is None:
        pct = 100.0
    if pct is None or pct <= 5.0:
        return _gate("R15", "벤젠 5% 초과 특별취급", False, None, "해당없음(벤젠 5% 이하)")
    certified = ctx.get("prev_cargo_benzene_free") is True
    return _gate("R15", "벤젠 5% 초과 특별취급", not certified, "HOLD",
                 "이전화물 벤젠프리 증명 확인" if certified
                 else f"벤젠 {pct}% 초과 화물 - 이전화물 벤젠프리 증명 필요, 미제출 -> 보류")


_GATES = [r1_sire, r2_cdi, r3_draught, r4_dwt, r5_loa, r6_work_hours, r7_upa_approval,
          r8_pilotage, r9_eta_report, r10_ssscl, r11_anchorage, r12_emergency,
          r13_flammable_adjacency, r14_lightning_heat, r15_benzene]


# ─── risk_level 결정론 함수 (GAP5) ───

def decide_risk_level(rule_engine_floor: str, seg_code: Optional[int],
                      flamm_grade: str, gate_results: list) -> str:
    """(rule_engine_floor, IMDG 격리코드, 인화성등급, 게이트 히트) -> risk_level.
    순수 max 연산만 사용 - 같은 입력이면 반드시 같은 등급."""
    level = rule_engine_floor if rule_engine_floor in RISK_ORDER else "안전"
    for g in gate_results:
        if g["hit"]:
            level = max(level, _SEVERITY_TO_RISK[g["severity"]], key=RISK_ORDER.index)
    if flamm_grade in {"저인화점", "인화성가스"}:
        level = max(level, "주의", key=RISK_ORDER.index)
    if seg_code is not None and seg_code >= 3:
        level = max(level, "주의", key=RISK_ORDER.index)
    return level


def compose_explanation(risk_level: str, gate_results: list, ctx: dict) -> dict:
    """설명 문장 생성 자리. LLM 을 붙이더라도 이 함수 출력(문장)만 대체하며
    risk_level 판정에는 관여하지 않는다. 현재는 결정론 템플릿."""
    hits = [g for g in gate_results if g["hit"]]
    checklist = list(ctx.get("base_checklist") or [])
    if ctx.get("benzene_pct") or ctx["cargo_name"] == "벤젠":
        checklist.append("벤젠 함유 화물 특별취급 절차 확인 (정일 9.18 / OTK 9.17)")
        checklist.append("H2S 발생 가능성 및 가스 감지 확인 (정일 9.19 / OTK 9.18)")
    summary = (f"위험등급 '{risk_level}': 게이트 {len(_GATES)}개 중 {len(hits)}건 히트"
               if hits else f"위험등급 '{risk_level}': 전체 게이트 통과")
    return {
        "summary": summary,
        "reasoning": [f"[{g['rule']}] {g['reason']}" for g in hits] or ["전 규칙 통과"],
        "checklist": checklist,
    }


def run_safety_gates(arrival: dict) -> dict:
    """신규 입항 안전 판정. R1~R15 전부 실행 + risk_level 결정론 확정.

    arrival 주요 키: cargo_name(필수), berth_name, dwt, gt, draught_m, loa_m,
      sire_valid, cdi_valid, work_hour, eta_reported_hours_before, minutes_since_ssscl,
      anchorage, pilot_onboard, international, emergency{...}, weather{lightning, temp_c},
      adjacent_operations[{berth_name, cargo_name, activity}], benzene_pct,
      prev_cargo_benzene_free
    """
    cargo = arrival.get("cargo_name") or ""
    info = _CARGO_MAP.get(cargo) or {}
    ctx = dict(arrival)
    ctx["cargo_name"] = cargo
    ctx["cargo_category"] = info.get("category", "미등록")
    ctx["flammability_grade"] = flammability_grade(cargo)
    ctx["berth_id"] = _NAME2ID.get(arrival.get("berth_name") or "")

    gate_results = [g(ctx) for g in _GATES]

    # rule_engine_floor: 기존 혼재금지 룰엔진(그래프 INCOMPATIBLE_WITH 대응)의 하한
    floor = "안전"
    floor_reasons = []
    for op in ctx.get("adjacent_operations") or []:
        if not op.get("cargo_name"):
            continue
        ev = evaluate_safety(cargo, op["cargo_name"])
        if not ev["safe"]:
            floor = "위험"
            floor_reasons.append(ev["reason"] + f" (인접: {op.get('berth_name')})")
    ctx["base_checklist"] = (CARGO_MSDS_DB.get(cargo) or {}).get("안전체크리스트", [])

    # IMDG 격리코드: 인접 작업 중 최대 격리코드
    seg_codes = [segregation_code(info.get("imdg_class", ""),
                                  (_CARGO_MAP.get(op.get("cargo_name") or "") or {}).get("imdg_class", ""))
                 for op in ctx.get("adjacent_operations") or [] if op.get("cargo_name") in _CARGO_MAP]
    seg_code = max(seg_codes) if seg_codes else None

    risk_level = decide_risk_level(floor, seg_code, ctx["flammability_grade"], gate_results)

    return {
        "risk_level": risk_level,
        "risk_level_basis": {
            "rule_engine_floor": floor,
            "floor_reasons": floor_reasons,
            "imdg_segregation_code": seg_code,
            "flammability_grade": ctx["flammability_grade"],
            "gate_hits": [g["rule"] for g in gate_results if g["hit"]],
        },
        "gates": gate_results,
        "explanation": compose_explanation(risk_level, gate_results, ctx),
    }
