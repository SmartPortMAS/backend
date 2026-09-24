"""입항 감시 → **판정 기록** (10분 주기).

[2026-09-21 전면 개편] 이 잡은 더 이상 선석을 배정하지 않는다.

이전 판은 `berth_assignment`(예약) · `anchorage_queue`(대기열) · `scheduling_exclusion`
(제외 사유) 세 표에 썼다. 셋 다 **배정 주체가 쓰는 표**다. 우리는 배정 주체가
아니다(9/17 회의 §1: "기존 선석 배정은 그대로 따른다"). 지금은 표 하나에만 쓴다.

    assessment_history — "이 배가 지금 있는 자리가 조건에 맞는가"

배정은 항만공사 선석회의가, 항내 이동 통제는 VTS 가, 하역 개시·중단은 터미널이
한다(회의 §2). 우리는 그 셋에게 근거를 넘긴다 — `action` 과 `recipient` 가 그것이다.

───────────────────────────────────────────────────────────────────────────
검증 대상: **PORT-MIS 배정 ∪ AIS 실제 접안** (P1-C, 2026-09-21)

두 소스 모두 "이 배의 자리"에 대한 **관측**이지 결정이 아니다. 그래서 둘을
합쳐도 방향 C 와 충돌하지 않는다 — 우리는 자리를 고르는 게 아니라 읽을 뿐이다.

합쳐야 하는 이유는 실측이다. 2026-09-21 기준 실제 계류 중인 액체화물선 10척 중
PORT-MIS 가 같은 선석을 적고 있는 배는 **1척**뿐이었다. 나머지는 PORT-MIS 가
아직 '정박지'라고 적고 있는데 AIS 는 이미 부두에 붙어 있다고 말한다 — 원유운반선
BOCT5, LPG 운반선 D8MP 등이 여기 해당했다. PORT-MIS 만 보면 이들이 통째로
검증에서 빠진다.

어느 소스에서 왔는지는 `input_snapshot.source` 에 남긴다. 둘이 어긋나는 것 자체가
신고 정정이 필요하다는 신호이므로, 사후에 셀 수 있어야 한다.
───────────────────────────────────────────────────────────────────────────

시점(stage)은 AIS 항해상태로만 정한다. PORT-MIS 는 수집창 `[어제, 오늘+3일]` 밖이면
동결되기 때문이다 — 자세한 실측은 `app/models/assessment_history.py::AssessmentStage`.

DWT 는 이 자동 흐름에서 항상 None 이다 — 어떤 실시간 소스도 재화중량톤수를 주지
않는다(총톤수·순톤수만 있고 DWT 와는 다른 값). "모르면 판정하지 않는다"가 아니라
"모르면 DWT 축을 건너뛴다"이므로 안전 문제는 아니다.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.agents.orchestrator.schemas import OrchestratorRequest
from app.agents.orchestrator.service import orchestrate
from app.agents.safety.schemas import CargoRef
from app.agents.scheduling.schemas import VesselSpec
from app.database import AsyncSessionFactory
from app.llm.factory import get_llm_client
from app.models.assessment_history import AssessmentLevel
from app.neo4j_client import neo4j_client
from app.services.assessment import (
    record_assessment,
    record_from_orchestrator,
    stage_from_nav_status,
)

logger = logging.getLogger("arrival_watcher")

DEFAULT_WINDOW_HOURS = 24  # 재항 이력 표본이 없는 선석에 쓰는 기본 접안 예상 기간(추정치)
MIN_FORWARD_HOURS = 12  # 판정 창이 만들어지자마자 과거로 끝나지 않게 두는 최소 여유
# 입항 직후라 아직 위치 수집(EC2 매시)에 안 잡혔을 수 있는 배는 이 시간 동안은 위치
# 없이도 대상에 둔다(아래 present 조건).
PRESENCE_GRACE_HOURS = 24


_QUERY_ASSESSMENT_TARGETS = text("""
    WITH live AS (
        -- [2026-09-22] berth_occupancy_live -> mart.vessel_presence.
        --   같은 질문에 뷰가 둘이라 답이 갈렸다(실측 같은 스냅샷: 14척 vs 0척).
        --   전자는 now() 기준 30분 안의 위치만 접안으로 봐서, 수집이 밀리면
        --   판정 대상이 통째로 사라졌다 — 이 job 은 그러면 아무 배도 판정하지
        --   않고 조용히 끝난다. vessel_presence 는 최신 스냅샷 기준이다.
        --
        --   뷰가 이미 선박당 1행이고 berth_name 이 정본 표기라 facility_alias 를
        --   다시 태우면 안 된다 — alias 는 wharf_name 당 source_name 이 여러 개라
        --   조인하면 배가 불어난다(2026-09-20 실측: 2부두 3척 -> 9척).
        SELECT upper(btrim(vp.callsgn)) AS cs,
               vp.berth_name AS wharf_name, vp.berth_dist_m AS distance_m
        FROM mart.vessel_presence vp
        WHERE vp.presence_zone = 'BERTH'
          AND nullif(btrim(vp.berth_name), '') IS NOT NULL
          AND nullif(btrim(vp.callsgn), '') IS NOT NULL
    ),
    -- [2026-09-22] "지금 항내에 있는 배"만 (가) 대상으로 둔다.
    --   PORT-MIS 는 수집창 밖이면 동결돼 출항 신고가 영영 안 오는 행이 남는다
    --   (AssessmentStage 주석의 191건과 같은 현상). 로컬 누적 DB 실측: 대상 241척 중
    --   최근 위치가 없는 배 195척, 입항 7일 초과 + 출항 없음 162척. 그 배들에 10분마다
    --   오케스트레이터(LLM)를 돌리고 판정 이력·확인 대기 목록을 채웠다.
    --   vessel_presence 는 스냅샷 3시간 안에 위치가 잡힌 배만 담는다(now() 가 아니라
    --   스냅샷 기준이라 수집이 밀려도 대상이 통째로 사라지지 않는다).
    --   입항 PRESENCE_GRACE_HOURS 안의 배는 위치가 아직 없어도 둔다.
    present AS (
        SELECT DISTINCT upper(btrim(callsgn)) AS cs
        FROM mart.vessel_presence
        WHERE nullif(btrim(callsgn), '') IS NOT NULL
    ),
    pm AS (
        SELECT DISTINCT ON (upper(btrim(callsgn))) upper(btrim(callsgn)) AS cs,
               collected_at_utc, arrival_report_type
        FROM portmis_vessel
        WHERE nullif(btrim(callsgn), '') IS NOT NULL
        ORDER BY upper(btrim(callsgn)), arrival_at_utc DESC NULLS LAST
    )
    SELECT
        dc.callsgn, dc.vessel_name, dc.imo_no, dc.draught AS draught_m,
        dc.arrival_at_utc, dc.nav_status_code, dc.received_at_utc AS position_at_utc,
        -- PORT-MIS 공식 배정 계선시설. '정박지-E1' 같은 정박지 배정도 여기로 온다.
        dc.facility_name AS assigned_facility_name,
        -- 그 배정 기록에 출항까지 신고돼 있으면 지난 입항의 기록이다(_target_wharf).
        dc.departure_at_utc AS assigned_departure_at_utc,
        lv.wharf_name AS moored_wharf_name,
        lv.distance_m AS moored_distance_m,
        pm.collected_at_utc AS portmis_collected_at,
        pm.arrival_report_type,
        (
            SELECT cm.chem_id FROM mart.cargo_msds cm
            WHERE cm.callsgn = dc.callsgn AND cm.chem_id IS NOT NULL
            LIMIT 1
        ) AS chem_id,
        (
            SELECT bds.median_hours
            FROM mart.facility_alias fa
            JOIN mart.berth_dwell_stats bds ON bds.wharf_name = fa.wharf_name
            WHERE fa.source_name = dc.facility_name AND fa.facility_type = 'BERTH'
            LIMIT 1
        ) AS median_dwell_hours
    FROM mart.dashboard_current dc
    LEFT JOIN live lv ON lv.cs = upper(btrim(dc.callsgn))
    LEFT JOIN pm ON pm.cs = upper(btrim(dc.callsgn))
    LEFT JOIN present pr ON pr.cs = upper(btrim(dc.callsgn))
    WHERE dc.is_liquid_cargo_vessel
      AND (
            -- (가) PORT-MIS 가 선석을 적어 둔 배 (정박지 배정은 대상이 아니다)
            (
                dc.arrival_at_utc IS NOT NULL
                AND dc.departure_at_utc IS NULL
                AND nullif(btrim(dc.facility_name), '') IS NOT NULL
                AND dc.facility_name NOT LIKE '%정박지%'
                AND (
                    pr.cs IS NOT NULL
                    OR dc.arrival_at_utc > now() - make_interval(hours => :grace_hours)
                )
            )
            -- (나) AIS 가 실제로 부두에 붙어 있다고 말하는 배.
            --      PORT-MIS 가 뭐라고 적었든, 출항 신고가 왔든 안 왔든 상관없다 —
            --      '지금 붙어 있다'가 관측이고 그게 판정 대상이다.
            OR lv.wharf_name IS NOT NULL
          )
    ORDER BY dc.arrival_at_utc ASC NULLS LAST
""")


def _target_wharf(row: dict) -> tuple[str | None, str]:
    """검증할 계류시설과 그 출처.

    PORT-MIS 배정을 먼저 본다 — 그게 공식 기록이고, AIS 는 아직 이동 중일 수 있다.
    PORT-MIS 가 정박지거나 비어 있으면 AIS 실측으로 내려간다.

    [2026-09-22] 그 배정 기록에 출항까지 신고돼 있으면 지난 입항의 기록이다 — 이번
    입항은 아직 PORT-MIS 에 없거나 수집창 밖이다. 그때는 실제로 붙어 있는 자리를 본다.
    실측: SUN BIRDIE 는 지금 정일1부두(107 m)에 붙어 있는데 7/25 입·출항 기록의
    '정일스톨트헤븐울산신항3부두'로 하역중 판정을 받았다.
    """
    assigned = (row.get("assigned_facility_name") or "").strip()
    closed = row.get("assigned_departure_at_utc") is not None
    if assigned and "정박지" not in assigned and not closed:
        return assigned, "PORT-MIS"
    moored = (row.get("moored_wharf_name") or "").strip()
    if moored:
        return moored, "AIS"
    return None, "없음"


def _snapshot(row: dict, *, source: str, target: str | None) -> dict:
    """판정을 재현하는 데 필요한 관측의 시각과 값.

    `portmis_collected_at` 은 선택이 아니다 — PORT-MIS 는 수집창 밖이면 동결되므로
    이 값이 없으면 '며칠 묵은 배정으로 내려진 판정인가'를 사후에 가릴 수 없다.
    """

    def _iso(value) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else None

    return {
        "source": source,
        "target_facility": target,
        "portmis_facility": row.get("assigned_facility_name"),
        "portmis_collected_at": _iso(row.get("portmis_collected_at")),
        "portmis_report_type": row.get("arrival_report_type"),
        "ais_moored_wharf": row.get("moored_wharf_name"),
        "ais_distance_m": (
            float(row["moored_distance_m"]) if row.get("moored_distance_m") is not None else None
        ),
        "position_at_utc": _iso(row.get("position_at_utc")),
        "nav_status_code": row.get("nav_status_code"),
        "draught_m": float(row["draught_m"]) if row.get("draught_m") is not None else None,
        "arrival_at_utc": _iso(row.get("arrival_at_utc")),
    }


async def watch_arrivals() -> None:
    """검증 대상을 순회하며 판정을 `assessment_history` 에 남긴다."""
    llm_client = get_llm_client()

    async with AsyncSessionFactory() as db:
        rows = [
            dict(r)
            for r in (
                await db.execute(_QUERY_ASSESSMENT_TARGETS, {"grace_hours": PRESENCE_GRACE_HOURS})
            ).mappings().all()
        ]

    if not rows:
        logger.info("watch_arrivals: 검증 대상 없음")
        return

    logger.info("watch_arrivals: %d척 판정 시작", len(rows))
    recorded = skipped = unknown = 0

    for row in rows:
        callsgn = row["callsgn"]
        # mart.dashboard_current.imo_no 는 소스에 따라 정수로 올 때가 있다(2026-08-19 실측).
        if row.get("imo_no") is not None:
            row["imo_no"] = str(row["imo_no"])

        target, source = _target_wharf(row)
        snapshot = _snapshot(row, source=source, target=target)
        stage = stage_from_nav_status(row.get("nav_status_code"))

        # ── 판정불가 네 갈래 ────────────────────────────────────────────────
        # 회의 §4 "근거 부족을 안전과 구분". 모르는 것을 '적합'으로 밀지 않는다.
        # 실측 근거: 오늘 백테스트 표본 125건 중 흘수 정보가 아예 없는 건이
        # 51건(40.8%)이었다 — 이걸 통과시키면 40%를 근거 없이 통과시키는 것이다.
        blocker: str | None = None
        if target is None:
            blocker = "계류시설을 특정할 수 없습니다(PORT-MIS 정박지·미배정, AIS 접안 미탐지)"
        elif stage is None:
            blocker = "AIS 항해상태가 없어 지금 어느 시점인지 판단할 수 없습니다"
        elif not row.get("chem_id"):
            blocker = "적재 화물을 식별할 수 없습니다(MSDS 매칭 없음)"
        elif not row.get("draught_m") or float(row["draught_m"]) <= 0:
            blocker = "흘수 정보가 없어 수심 여유를 계산할 수 없습니다"

        if blocker is not None:
            async with AsyncSessionFactory() as db:
                wrote = await record_assessment(
                    db, call_sign=callsgn, vessel_name=row.get("vessel_name"),
                    stage=stage, wharf_name=target, level=AssessmentLevel.UNKNOWN,
                    headline=blocker, input_snapshot=snapshot,
                )
                await db.commit()
            unknown += 1
            recorded += int(wrote)
            skipped += int(not wrote)
            logger.info("watch_arrivals: %s 판정불가 - %s", callsgn, blocker)
            continue

        # ── 판정 창 ────────────────────────────────────────────────────────
        # 끝은 "이 배가 언제 자리를 비우는가"다. 출항 예정 시각이 원천에 없으므로
        # 그 선석의 실제 재항 이력 중앙값(mart.berth_dwell_stats)으로 잡되, 이미
        # 중앙값을 넘긴 배도 있으므로 최소 한 주기는 살아 있게 한다.
        now = datetime.now(timezone.utc)
        window_start = row.get("arrival_at_utc") or now
        dwell_h = row.get("median_dwell_hours") or DEFAULT_WINDOW_HOURS
        window_end = max(
            window_start + timedelta(hours=float(dwell_h)),
            now + timedelta(hours=MIN_FORWARD_HOURS),
        )

        request = OrchestratorRequest(
            vessel=VesselSpec(
                draught_m=row["draught_m"], dwt_t=None, name_hint=row.get("vessel_name"),
                call_sign=callsgn,
            ),
            cargo=CargoRef(chem_id=row["chem_id"]),
            window_start=window_start,
            window_end=window_end,
            # 검증모드 고정 — 이 시설 하나만 확인한다. None 을 넘기면 오케스트레이터가
            # 탐색모드로 떨어져 선석 top-3 를 새로 고른다. 그건 배정이다.
            assigned_wharf_name=target,
        )
        logger.info(
            "watch_arrivals: %s 검증 (%s / 출처 %s / 시점 %s)",
            callsgn, target, source, stage.value,
        )

        async with AsyncSessionFactory() as db:
            try:
                result = await orchestrate(db, neo4j_client.driver, llm_client, request)
            except Exception:
                logger.exception("watch_arrivals: %s 오케스트레이터 호출 실패", callsgn)
                continue

            wrote = await record_from_orchestrator(
                db, call_sign=callsgn, vessel_name=row.get("vessel_name"),
                stage=stage, wharf_name=target, result=result, input_snapshot=snapshot,
            )
            await db.commit()

        recorded += int(wrote)
        skipped += int(not wrote)
        logger.info(
            "watch_arrivals: %s -> %s%s",
            callsgn, result.overall_decision.value, "" if wrote else " (변화 없음, 기록 생략)",
        )

    logger.info(
        "watch_arrivals: 완료 — 대상 %d척 · 기록 %d건 · 변화없음 %d건 · 판정불가 %d척",
        len(rows), recorded, skipped, unknown,
    )
