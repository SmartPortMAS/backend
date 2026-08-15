"""선석 재항 현황에서 관제 경고를 생성한다 (요청 없이 도는 상시 감시).

기존 /safety/assess 는 "이 화물을 여기 배정해도 되나"를 묻는 **요청형**이다.
관제 화면의 경고 센터는 아무도 묻지 않아도 "지금 위험한 조합이 이미 붙어 있는가"를
띄워야 하므로, 같은 규칙 엔진을 재항 현황 전체에 돌리는 경로가 따로 필요하다.

[설계 원칙]
1. **판정 로직을 새로 만들지 않는다.** 혼재금지/IMDG 격리 판정의 권위는
   safety 에이전트 하나뿐이다. 여기서는 그 함수(find_incompatible_conflicts /
   find_imdg_segregation_conflicts / compute_*_floor)를 그대로 호출한다.
   두 벌이 되면 화면 경고와 챗봇 답이 어긋난다.
2. **LLM 을 부르지 않는다.** rule_engine_floor 는 LLM 과 무관하게 계산되므로
   (service.assess_safety 참고) 규칙 판정만으로 등급이 나온다. 상시 감시가
   화면을 열 때마다 LLM 을 선석 수만큼 호출하면 느리고 비싸다. 대신 LLM 설명이
   필요하면 사용자가 그 선석을 눌러 /safety/assess 를 부르면 된다.
3. **모르는 것을 위험으로도 안전으로도 단정하지 않는다.** chem_id 가 없는 화물
   (위험물인데 정체 미확인)은 판정 대상에서 빼되, 그 사실 자체를 경고로 올린다.
"""

from neo4j import AsyncDriver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .graph_queries import find_imdg_segregation_conflicts, find_incompatible_conflicts
from .rule_engine import compute_imdg_floor, compute_risk_floor
from .schemas import RiskLevel, max_risk_level, risk_level_rank

# 같은 선석에 함께 재항 중인 화물 목록. chem_id 가 있는 것만 판정에 쓰지만,
# 없는 것도(정체 미확인) 세어서 따로 알린다.
_QUERY_BERTH_CARGO = text("""
    SELECT facility_name, callsgn, chem_id, cargo_name, imdg_class
    FROM mart.berth_current_cargo
    WHERE facility_name IS NOT NULL
    ORDER BY facility_name
""")

# 흘수 판정은 뷰가 이미 결론을 내려뒀다 — 여기서는 옮겨 적기만 한다.
_QUERY_DRAUGHT_ALERTS = text("""
    SELECT callsgn, facility_name, ukc_m, ukc_required_m, draught_verdict
    FROM mart.berth_draught_check
    WHERE draught_verdict IN ('NOT_ALLOWED', 'MARGINAL')
    ORDER BY ukc_m NULLS LAST
""")

_LEVEL_TO_ALERT = {
    RiskLevel.BLOCKED: "DANGER",
    RiskLevel.DANGER: "DANGER",
    RiskLevel.CAUTION: "WARNING",
    RiskLevel.SAFE: "INFO",
}


async def _segregation_alerts(db: AsyncSession, driver: AsyncDriver) -> list[dict]:
    """같은 선석에 있는 화물끼리 짝을 지어 혼재금지·IMDG 격리 규칙을 돌린다."""
    rows = (await db.execute(_QUERY_BERTH_CARGO)).mappings().all()

    by_berth: dict[str, list[dict]] = {}
    for row in rows:
        by_berth.setdefault(row["facility_name"], []).append(dict(row))

    alerts: list[dict] = []
    # 선석별 충돌 쌍을 모았다가 마지막에 한 건으로 묶는다
    pairs_by_berth: dict[str, list[dict]] = {}
    for berth, cargos in by_berth.items():
        identified = [c for c in cargos if c["chem_id"]]
        unidentified = len(cargos) - len(identified)

        if unidentified:
            alerts.append({
                "level": "WARNING",
                "type": "UNIDENTIFIED_CARGO",
                "berth_name": berth,
                # "위험물인데 정체 미확인"은 "위험물이 아님"과 다른 정보다.
                "message": f"{berth}: 위험물 {unidentified}건이 물질 미확인 상태 — 혼재 판정 불가",
                "risk_level": None,
                "basis": "mart.berth_current_cargo.chem_id 결측",
            })

        # 짝이 없으면 혼재를 따질 대상 자체가 없다
        if len(identified) < 2:
            continue

        seen: set[tuple[str, str]] = set()
        for target in identified:
            others = [c for c in identified if c["chem_id"] != target["chem_id"]]
            if not others:
                continue
            other_ids = list({c["chem_id"] for c in others})

            raw_conflicts = await find_incompatible_conflicts(
                driver, target_chem_id=target["chem_id"], adjacent_chem_ids=other_ids
            )
            raw_imdg = await find_imdg_segregation_conflicts(
                driver, target_chem_id=target["chem_id"], adjacent_chem_ids=other_ids
            )
            if not raw_conflicts and not raw_imdg:
                continue

            floor = max_risk_level(
                compute_risk_floor(raw_conflicts), compute_imdg_floor(raw_imdg)
            )
            if risk_level_rank(floor) < risk_level_rank(RiskLevel.CAUTION):
                continue

            name_of = {c["chem_id"]: (c["cargo_name"] or c["chem_id"]) for c in identified}
            for raw in raw_conflicts + raw_imdg:
                other_id = raw["chem_id"]
                # (A,B)와 (B,A)는 같은 사건이라 한 번만 올린다
                pair = tuple(sorted((target["chem_id"], other_id)))
                if pair in seen:
                    continue
                seen.add(pair)

                target_name = target["cargo_name"] or target["chem_id"]
                other_name = raw.get("name_ko") or name_of.get(other_id, other_id)
                if "segregation_code" in raw:
                    why = f"IMDG 격리코드 {raw['segregation_code']}"
                else:
                    why = f"혼재금지({raw.get('category', '분류 미상')})"

                pairs_by_berth.setdefault(berth, []).append({
                    "level": _LEVEL_TO_ALERT[floor],
                    "risk_level": floor,
                    "text": f"{target_name} ↔ {other_name} {why}",
                })

    # 선석 단위로 묶는다.
    #
    # 한 선석에 위험물이 여러 종 재항하면 쌍의 수가 제곱으로 늘어난다(실측: 화물
    # 배정을 액체화물선 전수로 넓히자 121건). 관제사는 쌍이 아니라 **선석 단위로**
    # 조치하므로, 선석마다 가장 심각한 조합을 대표로 올리고 나머지는 건수로 알린다.
    # 전체 목록은 details 에 그대로 담아 화면이 펼쳐 볼 수 있게 한다 — 묶는 것이지
    # 버리는 것이 아니다.
    for berth, pairs in pairs_by_berth.items():
        worst = max(pairs, key=lambda x: risk_level_rank(x["risk_level"]))
        more = len(pairs) - 1
        suffix = f" 외 {more}쌍" if more else ""
        alerts.append({
            "level": worst["level"],
            "type": "SEGREGATION",
            "berth_name": berth,
            "message": f"{berth}: {worst['text']} → {worst['risk_level'].value}{suffix}",
            "risk_level": worst["risk_level"].value,
            "basis": "safety 규칙엔진(Neo4j 혼재금지 + IMDG 격리표)",
            "pair_count": len(pairs),
            "details": [p["text"] for p in pairs],
        })
    return alerts


async def _draught_alerts(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(_QUERY_DRAUGHT_ALERTS)).mappings().all()
    out = []
    for row in rows:
        not_allowed = row["draught_verdict"] == "NOT_ALLOWED"
        ukc = f"UKC {row['ukc_m']:.2f} m" if row["ukc_m"] is not None else "UKC 산출 불가"
        out.append({
            "level": "DANGER" if not_allowed else "WARNING",
            "type": "DRAUGHT",
            "berth_name": row["facility_name"],
            "message": f"{row['facility_name'] or '부두 미상'}: {row['callsgn']} "
                       f"흘수 여유 부족 ({ukc}, {row['draught_verdict']})",
            "risk_level": None,
            "basis": "mart.berth_draught_check (조위 반영 가용수심)",
        })
    return out


async def build_berth_alerts(db: AsyncSession, driver: AsyncDriver) -> list[dict]:
    """관제 경고 목록. 심각한 것부터 정렬해서 반환한다.

    경고가 0건인 것은 정상이다 — 없는 위험을 지어내지 않는다.
    """
    alerts = await _segregation_alerts(db, driver) + await _draught_alerts(db)
    order = {"DANGER": 0, "WARNING": 1, "INFO": 2}
    alerts.sort(key=lambda a: (order.get(a["level"], 9), a["type"]))
    return alerts
