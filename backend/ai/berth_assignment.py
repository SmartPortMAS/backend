"""
선석 배정 로직: 전용 -> 대체 -> 정박지 대기 (P3, 선석대체_정박지대기_모델.md)

액체 부두는 파이프라인이 탱크단지에 고정된 전용부두라 대체는 같은 운영사 안으로만
가능하고, 없으면 정박지 대기다. 판정 순서:
  1. 전용 선석이 비어 있으면 배정
  2. 점유 중이면 SUBSTITUTABLE_WITH 후보를 런타임 게이트로 검사
     (product in shared_products AND dwt <= to_max_dwt AND draught <= to_depth_m)
     단, 단독선석(효성/달포/석유공사부이)은 대체 단계 없이 바로 대기
  3. 대체도 없으면 assign_anchorage(dwt) 로 톤수 맞는 정박지 대기

데이터/함수는 onsan_mvp 를 재사용한다:
  - assign_anchorage: scripts/build_anchorage_assignment.py
  - 대체 후보: data/onsan_berth_substitutability.csv
  - 단독선석: data/onsan_berth_substitution_summary.csv (anchorage_only=Y)
판정은 결정론이며, 출력 trace 에 전용/대체/대기 판단 경로와 근거를 남긴다.
"""
import csv
import logging
import os
import sys
from typing import Optional

from simulator.data_generator import BERTH_LIST

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ONSAN_SCRIPTS = os.path.join(_REPO_ROOT, "onsan_mvp", "scripts")
_ONSAN_DATA = os.path.join(_REPO_ROOT, "onsan_mvp", "data")
if _ONSAN_SCRIPTS not in sys.path:
    sys.path.insert(0, _ONSAN_SCRIPTS)

from build_anchorage_assignment import assign_anchorage  # noqa: E402

_NAME2ID = {b["name"]: b["berth_id"] for b in BERTH_LIST}
_ID2NAME = {b["berth_id"]: b["name"] for b in BERTH_LIST}

# 유류 계열 화물명 (파이프라인 근사용. 그 외 액체는 케미칼, 원유는 원유로 분류)
_OIL_CARGOS = {"나프타", "등유", "경유", "중유", "벙커C유", "LPG", "휘발유", "항공유"}


def _num(v) -> Optional[float]:
    v = (str(v) if v is not None else "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _load_substitutes() -> dict:
    """{from_berth_id: [후보 행]} - onsan_berth_substitutability.csv"""
    table: dict = {}
    with open(os.path.join(_ONSAN_DATA, "onsan_berth_substitutability.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            table.setdefault(r["from_berth"].strip(), []).append({
                "to_berth": r["to_berth"].strip(),
                "shared_products": (r.get("shared_products") or "").strip(),
                "to_max_dwt": _num(r.get("to_max_dwt")),
                "to_depth_m": _num(r.get("to_depth_m")),
            })
    return table


def _load_singleton_ids() -> set:
    """단독선석(대체 불가, anchorage_only=Y) - onsan_berth_substitution_summary.csv"""
    with open(os.path.join(_ONSAN_DATA, "onsan_berth_substitution_summary.csv"), encoding="utf-8-sig") as f:
        return {r["berth_id"].strip() for r in csv.DictReader(f)
                if (r.get("anchorage_only") or "").strip().upper() == "Y"}


_SUBSTITUTES = _load_substitutes()
_SINGLETON_IDS = _load_singleton_ids()


def product_family(cargo_name: str) -> str:
    """화물명 -> 파이프라인 화물계열 근사 (원유/유류/케미칼)."""
    if cargo_name == "원유":
        return "원유"
    if cargo_name in _OIL_CARGOS:
        return "유류"
    return "케미칼"


def resolve_berth_assignment(
    target_berth_name: str,
    cargo_name: str,
    dwt: Optional[float],
    draught: Optional[float],
    berth_statuses: list,
) -> dict:
    """전용 -> 대체 -> 정박지 대기 판정.

    반환: {"path": "전용"|"대체"|"정박지대기", "assigned_berth": 선석명|None,
           "anchorage": 정박지id|None, "trace": [판단 근거]}
    """
    trace = []
    status_by_name = {b["berth_name"]: b for b in berth_statuses}
    target = status_by_name.get(target_berth_name)
    target_id = _NAME2ID.get(target_berth_name)

    # 1단계: 전용 선석
    if target is not None and not target["is_occupied"]:
        trace.append(f"전용 선석 '{target_berth_name}' 가용 -> 배정")
        return {"path": "전용", "assigned_berth": target_berth_name, "anchorage": None, "trace": trace}
    trace.append(f"전용 선석 '{target_berth_name}' 점유 중"
                 + (f" (현재 {target['current_vessel']}/{target['current_cargo']})" if target else " 또는 상태 불명"))

    # 2단계: 같은 운영사 대체 (단독선석은 건너뜀)
    if target_id in _SINGLETON_IDS:
        trace.append(f"'{target_berth_name}'은 단독선석(대체 불가) -> 대체 탐색 없이 정박지 대기")
    else:
        family = product_family(cargo_name)
        for cand in _SUBSTITUTES.get(target_id, []):
            cand_name = _ID2NAME.get(cand["to_berth"], cand["to_berth"])
            if family not in cand["shared_products"]:
                trace.append(f"대체후보 '{cand_name}' 탈락: 화물계열 {family} ∉ {cand['shared_products']}")
                continue
            if dwt is not None and cand["to_max_dwt"] is not None and dwt > cand["to_max_dwt"]:
                trace.append(f"대체후보 '{cand_name}' 탈락: DWT {dwt:,.0f} > 최대 {cand['to_max_dwt']:,.0f}")
                continue
            if draught is not None and cand["to_depth_m"] is not None and draught > cand["to_depth_m"]:
                trace.append(f"대체후보 '{cand_name}' 탈락: 흘수 {draught}m > 수심 {cand['to_depth_m']}m")
                continue
            cand_status = status_by_name.get(cand_name)
            if cand_status is None or cand_status["is_occupied"]:
                trace.append(f"대체후보 '{cand_name}' 탈락: 점유 중")
                continue
            trace.append(f"대체 선석 '{cand_name}' 게이트 통과(화물계열/DWT/흘수) + 가용 -> 배정")
            return {"path": "대체", "assigned_berth": cand_name, "anchorage": None, "trace": trace}
        trace.append("같은 운영사 내 가용 대체 선석 없음 -> 정박지 대기")

    # 3단계: 톤수 -> 정박지 (assign_anchorage 재사용)
    anchorage = assign_anchorage(dwt)
    trace.append(f"assign_anchorage(dwt={dwt}) -> '{anchorage}' 대기")
    return {"path": "정박지대기", "assigned_berth": None, "anchorage": anchorage, "trace": trace}
