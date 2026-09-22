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

from .bulk_compatibility import build_bulk_conflicts
from .graph_queries import (
    find_adjacent_berth_conflicts,
    find_bulk_exceptions,
    find_bulk_group_conflicts,
    find_bulk_groups,
    find_imdg_no_segregation_required,
    find_imdg_segregation_conflicts,
    find_incompatible_conflicts,
)
from .rule_engine import (
    compute_bulk_compatibility_floor,
    compute_imdg_berth_adjacency_floor,
    compute_imdg_costowage_floor,
    compute_imdg_unconfirmed_floor,
    compute_risk_floor,
)
from .schemas import RiskLevel, max_risk_level, risk_level_rank

# 같은 선석에 함께 재항 중인 화물 목록. chem_id 가 있는 것만 판정에 쓰지만,
# 없는 것도(정체 미확인) 세어서 따로 알린다.
#
# [2026-09-22] wharf_name(선석 제원 마스터 정본 표기)을 함께 싣는다 — D3 인접
#   선석 확장(_adjacent_berth_alerts)이 Neo4j `(:Berth {wharf_name})` 를 찾을 때
#   쓴다. facility_name 은 VTS/manifest 원문이라 그래프 키와 글자가 다르다.
#   facility_alias 는 source_name 이 유일키(idx_facility_alias_source)라 이
#   조인으로 행이 불어나지 않는다. BERTH 가 아닌 것(호안·정박지·미해소)은
#   wharf_name 이 NULL 로 남고, 인접 판정에서 조용히 빠진다 — 없는 선석의
#   인접을 지어내지 않기 위해서다.
_QUERY_BERTH_CARGO = text("""
    SELECT bcc.facility_name, bcc.callsgn, bcc.chem_id, bcc.cargo_name, bcc.imdg_class,
           CASE WHEN fa.facility_type = 'BERTH' THEN fa.wharf_name END AS wharf_name
    FROM mart.berth_current_cargo bcc
    LEFT JOIN mart.facility_alias fa ON fa.source_name = bcc.facility_name
    WHERE bcc.facility_name IS NOT NULL
    ORDER BY bcc.facility_name
""")

# 흘수 판정은 뷰가 이미 결론을 내려뒀다 — 여기서는 옮겨 적기만 한다.
_QUERY_DRAUGHT_ALERTS = text("""
    SELECT callsgn, facility_name, ukc_m, ukc_required_m, draught_verdict, chart_depth_max_m
    FROM mart.berth_draught_check
    WHERE draught_verdict IN ('NOT_ALLOWED', 'MARGINAL', 'CHECK')
    ORDER BY ukc_m NULLS LAST
""")

# RiskLevel -> 경고 심각도. 아래 _ASSESSMENT_LEVEL_TO_ALERT(판정 이력 어휘)와는
# 다른 표다 — 2026-09-22 까지 둘 다 _LEVEL_TO_ALERT 라 이게 덮여 있었다.
_RISK_TO_ALERT = {
    RiskLevel.BLOCKED: "DANGER",
    RiskLevel.DANGER: "DANGER",
    RiskLevel.CAUTION: "WARNING",
    RiskLevel.SAFE: "INFO",
}


async def _segregation_alerts(rows: list[dict], driver: AsyncDriver) -> list[dict]:
    """같은 선석에 있는 화물끼리 짝을 지어 혼재금지·IMDG 격리 규칙을 돌린다.

    [2026-09-22] 재항 화물 조회를 build_berth_alerts 로 올렸다 — 인접 선석
    판정(_adjacent_berth_alerts)이 같은 행을 쓰기 때문이다. 같은 스냅샷을
    공유해야 두 경고가 서로 다른 시점의 현황을 말하지 않는다.
    """
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

            # 대상·인접 화물의 IMDG Class가 둘 다 알려져 있는데 위 조회에 안 걸린
            # 쌍 — SEGREGATE 관계가 없다는 게 "공인 규정상 X(개별 확인 필요)"인지
            # "이 Class 조합이 그래프 커버리지 밖"인지 구분이 안 되므로, 여기서
            # "충돌 없음 = 안전"으로 조용히 넘기지 않는다(safety.rule_engine.
            # compute_imdg_unconfirmed_floor와 동일한 원칙 — 판정 로직은 그쪽이
            # 유일한 권위이므로 등급 계산은 반드시 그 함수를 그대로 호출한다).
            confirmed_imdg_ids = {r["chem_id"] for r in raw_imdg}
            # NO_SEGREGATION_REQUIRED로 확정된 조합(같은 Class끼리 등)은 미확인
            # 대상에서 뺀다 — service.assess_safety와 동일한 이유(2026-08-21 발견,
            # 벤젠-가솔린처럼 같은 Class끼리마다 근거 없는 주의 경고가 나던 문제).
            confirmed_no_segregation_ids = await find_imdg_no_segregation_required(
                driver, target_chem_id=target["chem_id"], adjacent_chem_ids=other_ids
            )
            unconfirmed_imdg = [
                {
                    "chem_id": o["chem_id"],
                    "name_ko": o["cargo_name"],
                    "segregation_code": "X(개별확인필요)",
                }
                for o in others
                if o["imdg_class"]
                and target["imdg_class"]
                and o["chem_id"] not in confirmed_imdg_ids
                and o["chem_id"] not in confirmed_no_segregation_ids
            ]

            # 벌크 액체화학물질 호환성 그룹 참고축(bulk_compatibility.py) — MSDS
            # 텍스트 마이닝·IMDG 공인 격리표와 근거가 다른 제3의 신호. 2026-08-21부터
            # Neo4j 그래프 조회로 판정한다(chem_id 기준 — service.assess_safety와
            # 동일한 그래프·동일한 판정 함수를 그대로 재사용해, 이 화면과 개별 심사
            # API·챗봇이 같은 질문에 다른 답을 내지 않게 한다).
            bulk_group_rows = await find_bulk_group_conflicts(
                driver, target_chem_id=target["chem_id"], adjacent_chem_ids=other_ids
            )
            bulk_groups = await find_bulk_groups(driver, chem_ids=[target["chem_id"], *other_ids])
            bulk_safe_ids, bulk_blocked_ids = await find_bulk_exceptions(
                driver, target_chem_id=target["chem_id"], adjacent_chem_ids=other_ids
            )
            raw_bulk = build_bulk_conflicts(
                target_chem_id=target["chem_id"],
                adjacent_chem_ids=other_ids,
                adjacent_names={o["chem_id"]: (o["cargo_name"] or o["chem_id"]) for o in others},
                group_conflict_ids={row["chem_id"] for row in bulk_group_rows},
                groups_by_chem_id=bulk_groups,
                safe_exception_ids=bulk_safe_ids,
                blocked_exception_ids=bulk_blocked_ids,
            )

            if not raw_conflicts and not raw_imdg and not unconfirmed_imdg and not raw_bulk:
                continue

            floor = max_risk_level(
                max_risk_level(
                    max_risk_level(compute_risk_floor(raw_conflicts), compute_imdg_costowage_floor(raw_imdg)),
                    compute_imdg_unconfirmed_floor(bool(unconfirmed_imdg)),
                ),
                compute_bulk_compatibility_floor(raw_bulk),
            )
            if risk_level_rank(floor) < risk_level_rank(RiskLevel.CAUTION):
                continue

            name_of = {c["chem_id"]: (c["cargo_name"] or c["chem_id"]) for c in identified}
            for raw in raw_conflicts + raw_imdg + unconfirmed_imdg + raw_bulk:
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
                elif "reason" in raw:
                    why = f"벌크호환성그룹(참고) — {raw['reason']}"
                else:
                    why = f"혼재금지({raw.get('category', '분류 미상')})"

                pairs_by_berth.setdefault(berth, []).append({
                    "level": _RISK_TO_ALERT[floor],
                    "risk_level": floor,
                    "text": f"{target_name} ↔ {other_name} {why}",
                    # 이 조합에 실제로 걸린 배들. 화면이 경고에서 선박 상세로 갈
                    # 수 있게 하려면 선석 이름만으로는 부족하다.
                    "callsgns": [
                        c["callsgn"]
                        for c in identified
                        if c["chem_id"] in (target["chem_id"], other_id) and c["callsgn"]
                    ],
                    # 이 경고가 지목한 두 물질. 화면이 "이 경고를 심사"로 넘어갈 때
                    # 무엇을 폼에 넣어야 하는지가 이 값이다 — 없으면 그 선석의 아무
                    # 화물이나 집어넣게 되어, 경고는 "가솔린↔부탄"인데 심사는 케로젠을
                    # 하는 상황이 된다.
                    "chem_ids": [target["chem_id"], other_id],
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
        # 이 선석 경고에 걸린 배 전체(중복 제거, 등장 순서 유지)
        callsgns: list[str] = []
        for p in pairs:
            for cs in p.get("callsgns", []):
                if cs not in callsgns:
                    callsgns.append(cs)
        alerts.append({
            "level": worst["level"],
            "type": "SEGREGATION",
            "berth_name": berth,
            "message": f"{berth}: {worst['text']} → {worst['risk_level'].value}{suffix}",
            "risk_level": worst["risk_level"].value,
            "basis": "혼재금지 · IMDG 격리 · 벌크 호환성 기준",
            "pair_count": len(pairs),
            "details": [p["text"] for p in pairs],
            # 화면이 경고 → 선박 상세로 갈 수 있게 하는 유일한 키.
            # (port_call_id 는 이 경로에 존재하지 않는다 — 경고는 요청형 판정이
            #  아니라 재항 현황 전수 판정에서 나오기 때문이다.)
            "callsgns": callsgns,
            # 대표로 올린 조합(worst)의 두 물질 — "이 경고를 심사"가 재현해야 할 입력
            "chem_ids": worst.get("chem_ids", []),
        })
    return alerts


async def _adjacent_berth_alerts(rows: list[dict], driver: AsyncDriver) -> list[dict]:
    """D3 — **인접 부두** 재항 화물과의 충돌 (9/17 회의 §3, 목표일 9/23).

    위 _segregation_alerts 는 같은 부두 안만 본다. 여기는 그래프의
    (:Berth)-[:ADJACENT_TO]->(:Berth) 를 타고 옆 부두까지 넓힌다 — 그 간선
    120개(부두 단위 11쌍)는 그동안 백엔드가 한 번도 조회하지 않았다.

    ★ 등급 근거는 MSDS 축만 쓴다.
      IMDG Code Ch.7.2 는 단일 선박 내 적부 규정이라 부두와 부두 사이에는
      적용하지 않는다는 것이 이 프로젝트의 기존 판단이고(rule_engine.
      compute_imdg_berth_adjacency_floor — 항상 SAFE, 근거 MSC.1/Circ.1216),
      D3 라고 해서 그 판단을 뒤집지 않는다. 그래서 IMDG 로만 걸린 인접 조합은
      등급을 올리지 않고 INFO(참고)로만 올린다 — 조회는 계속 하되 판정에서만
      빼는, 그 함수가 지시하는 그대로의 취급이다.

    ★ 판정 로직을 새로 만들지 않는다(모듈 docstring 1번). 등급은 전부
      compute_risk_floor / compute_imdg_berth_adjacency_floor 가 낸다.
    """
    cargo = [r for r in rows if r.get("wharf_name") and r.get("chem_id")]
    if len(cargo) < 2:
        return []

    by_wharf: dict[str, list[dict]] = {}
    for c in cargo:
        by_wharf.setdefault(c["wharf_name"], []).append(c)

    # (이쪽 부두·물질, 저쪽 부두·물질)은 양방향으로 한 번씩 걸린다 — 같은 사건이다.
    seen: set[frozenset] = set()
    alerts: list[dict] = []

    for wharf, cargos in by_wharf.items():
        for target in {c["chem_id"]: c for c in cargos}.values():
            hits = await find_adjacent_berth_conflicts(
                driver,
                wharf_name=wharf,
                target_chem_id=target["chem_id"],
                neighbor_cargo=cargo,
            )
            for hit in hits:
                pair = frozenset({
                    (wharf, target["chem_id"]),
                    (hit["neighbor_wharf"], hit["chem_id"]),
                })
                if pair in seen:
                    continue
                seen.add(pair)

                msds = (
                    [{"category": hit["via"]}]
                    if hit["basis"] == "MSDS_INCOMPATIBLE"
                    else []
                )
                floor = max_risk_level(
                    compute_risk_floor(msds),
                    # 항상 SAFE. "IMDG 를 의도적으로 뺐다"가 코드에 보이게 남긴다.
                    compute_imdg_berth_adjacency_floor([hit]),
                )
                is_reference_only = floor == RiskLevel.SAFE

                target_name = target["cargo_name"] or target["chem_id"]
                dist = (
                    f"{int(round(hit['distance_m']))}m"
                    if hit["distance_m"] is not None
                    else "거리 미상"
                )
                if is_reference_only:
                    message = (
                        f"{wharf}: {target_name} ↔ 인접 {hit['neighbor_wharf']}"
                        f"({dist}) {hit['name_ko']} — IMDG 격리코드 "
                        f"{hit['segregation_code']} (참고 — 부두 간에는 등급 근거로 "
                        f"쓰지 않음)"
                    )
                else:
                    message = (
                        f"{wharf}: {target_name} ↔ 인접 {hit['neighbor_wharf']}"
                        f"({dist}) {hit['name_ko']} 혼재금지({hit['via']})"
                        f" → {floor.value}"
                    )

                callsgns = [
                    cs
                    for cs in (target.get("callsgn"), hit.get("callsgn"))
                    if cs
                ]
                alerts.append({
                    "level": "INFO" if is_reference_only else _RISK_TO_ALERT[floor],
                    "type": "ADJACENT_SEGREGATION",
                    "berth_name": wharf,
                    "message": message,
                    "risk_level": None if is_reference_only else floor.value,
                    "basis": "Neo4j ADJACENT_TO 확장 탐색 (선석→인접→재항화물→충돌)",
                    # 그래프가 만든 경로 문장. 화면·보고서가 근거를 다시 조립하지
                    # 않게 하려고 그대로 싣는다(회의 §3 "경로 문장").
                    "graph_path": hit["path_text"],
                    "neighbor_berth_name": hit["neighbor_wharf"],
                    "distance_m": hit["distance_m"],
                    "callsgns": list(dict.fromkeys(callsgns)),
                    "chem_ids": [target["chem_id"], hit["chem_id"]],
                })

    return alerts


async def _draught_alerts(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(_QUERY_DRAUGHT_ALERTS)).mappings().all()
    out = []
    for row in rows:
        verdict = row["draught_verdict"]
        not_allowed = verdict == "NOT_ALLOWED"
        ukc = f"UKC {row['ukc_m']:.2f} m" if row["ukc_m"] is not None else "UKC 산출 불가"
        if verdict == "CHECK":
            # 선석별 수심이 다른 부두(SK5 7~11m 등) — 가장 얕은 선석 기준으론 부족하지만
            # 깊은 선석이면 된다. VTS 기록에 선석 번호가 없어 시스템은 어느 선석인지
            # 모르므로 '불가'가 아니라 '확인 요청'이다(mart_views.sql berth_draught_check).
            message = (f"{row['facility_name'] or '부두 미상'}: {row['callsgn']} "
                       f"접안 선석 확인 요청 — 가장 얕은 선석 기준 {ukc}, "
                       f"가장 깊은 선석({row['chart_depth_max_m']} m)이면 여유 있음")
        else:
            message = (f"{row['facility_name'] or '부두 미상'}: {row['callsgn']} "
                       f"흘수 여유 부족 ({ukc}, {verdict})")
        out.append({
            "level": "DANGER" if not_allowed else ("INFO" if verdict == "CHECK" else "WARNING"),
            "type": "DRAUGHT",
            "berth_name": row["facility_name"],
            "message": message,
            "risk_level": None,
            "basis": "mart.berth_draught_check (조위 반영 가용수심)",
            # 흘수 경고는 배 한 척의 문제다 — 그 배로 바로 갈 수 있게 한다
            "callsgns": [row["callsgn"]] if row["callsgn"] else [],
        })
    return out


# [2026-09-21] scheduling_exclusion -> assessment_history 로 교체.
#
# 옛 표는 "자동 배정이 배정을 만들지 못한 건"의 목록이었다. 우리가 배정을 만들지
# 않으므로 못 만든 건도 없다(방향 C). 같은 자리를 채우는 것은 판정 이력이다 —
# 관제사가 알아야 하는 건 '배정 실패'가 아니라 **'지금 자리가 조건에 안 맞는 배'** 다.
#
# 배 1척당 최신 판정 1건만 본다. 이력 전체를 펼치면 같은 배가 화면에 여러 번 뜬다.
_QUERY_ACTIVE_ASSESSMENTS = text("""
    SELECT DISTINCT ON (call_sign)
           call_sign, vessel_name, stage, wharf_name, level, action, recipient,
           reasons, assessed_at_utc
    FROM assessment_history
    WHERE level <> '적합'
      -- 오래된 판정은 경고가 아니라 이력이다. 그 배는 이미 떠났을 수 있다.
      AND assessed_at_utc > now() - interval '24 hours'
    ORDER BY call_sign, assessed_at_utc DESC
""")

# 판정 등급 -> 경고 심각도. '판정불가'를 WARNING 으로 두는 것이 핵심이다 —
# 근거가 없다는 사실 자체를 관제사가 봐야 한다(회의 §4 "근거 부족을 안전과 구분").
# 조용히 넘기면 모르는 것을 통과시킨 것과 같아진다.
#
# ★ [2026-09-22] 이름을 _LEVEL_TO_ALERT 에서 바꿨다 — 이 파일 위쪽의 RiskLevel용
#   _LEVEL_TO_ALERT 와 이름이 같아 **뒤에 정의된 이 dict 가 앞엣것을 덮고 있었다**.
#   그래서 _segregation_alerts 의 `_LEVEL_TO_ALERT[floor]` 가 실제로는 이 표를
#   찾아갔다. RiskLevel 은 str Enum 이라 '주의'만 우연히 맞고, '위험'·'배정불가'는
#   KeyError 였다(실측: RiskLevel.DANGER/BLOCKED 둘 다 KeyError).
#   즉 같은 선석에 위험 이상 조합이 생기는 순간 /dashboard/alerts 가 통째로 500 이
#   된다 — 경고 화면이 가장 필요한 바로 그 순간에. 지금 200 인 것은 재항 조합이
#   우연히 '주의'까지만 올라가 있어서다(AssessmentLevel 은 별개 어휘라 무관).
_ASSESSMENT_LEVEL_TO_ALERT = {
    "부적합": "DANGER",
    "주의": "WARNING",
    "판정불가": "WARNING",
}


async def _assessment_alerts(db: AsyncSession) -> list[dict]:
    """최근 24시간 판정 중 '적합'이 아닌 것.

    여기서 새 판단을 만들지 않는다(모듈 docstring 1번) — 이미 내려진 판정을
    경고 형식으로 옮겨 적을 뿐이다.
    """
    rows = (await db.execute(_QUERY_ACTIVE_ASSESSMENTS)).mappings().all()
    out = []
    for row in rows:
        who = row["vessel_name"] or row["call_sign"]
        where = row["wharf_name"] or "계류시설 미상"
        head = (row["reasons"] or [""])[0]
        # 조치안이 있으면 받을 곳까지 적는다. 우리가 실행하지 않는다는 뜻이 문장에 남는다.
        tail = f" → {row['action']} 검토 필요({row['recipient']})" if row["action"] else ""
        out.append({
            "level": _ASSESSMENT_LEVEL_TO_ALERT.get(row["level"], "WARNING"),
            "type": f"ASSESSMENT_{row['level']}",
            "berth_name": row["wharf_name"],
            "message": f"[{row['stage']}] {who} @ {where}: {row['level']} — {head}{tail}",
            "risk_level": None,
            "basis": "assessment_history (판정 이력)",
            "callsgns": [row["call_sign"]] if row["call_sign"] else [],
        })
    return out


async def build_berth_alerts(db: AsyncSession, driver: AsyncDriver) -> list[dict]:
    """관제 경고 목록. 심각한 것부터 정렬해서 반환한다.

    경고가 0건인 것은 정상이다 — 없는 위험을 지어내지 않는다.
    """
    # 재항 화물은 한 번만 읽어 같은 선석 판정과 인접 선석 판정(D3)이 나눠 쓴다 —
    # 두 번 읽으면 두 경고가 서로 다른 시점의 현황을 말할 수 있다.
    cargo_rows = [dict(r) for r in (await db.execute(_QUERY_BERTH_CARGO)).mappings().all()]

    alerts = (
        await _segregation_alerts(cargo_rows, driver)
        + await _adjacent_berth_alerts(cargo_rows, driver)
        + await _draught_alerts(db)
        + await _assessment_alerts(db)
    )
    order = {"DANGER": 0, "WARNING": 1, "INFO": 2}
    alerts.sort(key=lambda a: (order.get(a["level"], 9), a["type"]))
    return alerts
