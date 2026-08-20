"""입항 이벤트 감지 → 자동 추천 (10분 주기).

08_스케줄링_전면재설계_자동배정_설계문서.md §5.1, §5.3 — 07 문서 §4.3이 설계한
"입항허가 완료 + 아직 배정 없는 선박" 쿼리를, 관제사 화면이 아니라 이 백그라운드
잡이 폴링해 오케스트레이터를 자동 호출하는 트리거로 쓴다.

원안(07 문서 §4.3)은 `mart.dashboard_current`(AIS)와 `portmis_vessel`을 여기서
다시 FULL OUTER JOIN하는 구조였지만, 실제로는 `mart.dashboard_current` 뷰 자체가
이미 그 조인을 끝내 둔 "한 줄 조회" 뷰라는 걸 확인했다(dashboard.py 주석,
2026-08-19 라이브 스키마 조회로 재확인) — 그래서 이 잡은 그 뷰 하나만 본다.
화물 식별은 `mart.cargo_msds`(호출부호 -> chem_id, 이미 UN번호까지 MSDS와
매칭해 둔 뷰)를 그대로 재사용한다.

DWT는 이 자동 흐름에서 항상 None이다 — 어떤 실시간 소스도 재화중량톤수를
제공하지 않는다(총톤수(GT)·순톤수만 있음, DWT와는 다른 값). "모르면 배정하지
않는다"가 아니라 "모르면 DWT 게이트를 건너뛴다"이므로(VesselSpec.dwt_t 자체가
선택 필드) 안전 문제는 아니다 — 다만 부이(VLCC 전용) 게이트는 dwt_t가 없으면
항상 탈락하므로(§4.1.3-A, 의도된 비대칭), 이 자동 흐름으로는 부이가 추천되는
일이 사실상 없다.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.schemas import OrchestratorRequest, OverallDecision
from app.agents.orchestrator.service import orchestrate
from app.agents.safety.schemas import CargoRef
from app.agents.scheduling.occupancy import find_free_slot
from app.agents.scheduling.schemas import VesselSpec
from app.database import AsyncSessionFactory
from app.llm.factory import get_llm_client
from app.models.anchorage_queue import STATUS_WAITING, AnchorageQueue
from app.models.berth_assignment import STATUS_REQUESTED, BerthAssignment
from app.models.scheduling_exclusion import SchedulingExclusion
from app.neo4j_client import neo4j_client

logger = logging.getLogger("arrival_watcher")

DEFAULT_WINDOW_HOURS = 24  # 재항 이력 표본이 없는 선석에 쓰는 기본 접안 예상 기간(추정치)
# 아직 출항하지 않은 배의 계획기간이 과거로 끝나지 않도록 두는 최소 여유.
# 이 값보다 짧게 잡으면 추천이 만들어진 직후 만료돼 화면에서 사라진다.
MIN_FORWARD_HOURS = 12

# §5.1 — 07 문서 §4.3의 NOT EXISTS 조건을 CANCELLED 뿐 아니라 REJECTED도 제외하도록
# 고쳤다(발견 경위는 설계문서 §5.1 참고) — 그래야 반려당한 선박이 다음 주기에
# 다시 후보로 잡힌다.
_QUERY_PENDING_ARRIVALS = text("""
    SELECT
        dc.callsgn, dc.vessel_name, dc.imo_no, dc.draught AS draught_m,
        dc.arrival_at_utc,
        (
            SELECT cm.chem_id FROM mart.cargo_msds cm
            WHERE cm.callsgn = dc.callsgn AND cm.chem_id IS NOT NULL
            LIMIT 1
        ) AS chem_id,
        -- 이 배가 붙어 있는 시설의 실측 재항 중앙값(없으면 NULL → 기본값 사용).
        -- 계획기간의 끝을 정하는 데 쓴다.
        (
            SELECT bds.median_hours
            FROM mart.facility_alias fa
            JOIN mart.berth_dwell_stats bds ON bds.wharf_name = fa.wharf_name
            WHERE fa.source_name = dc.facility_name AND fa.facility_type = 'BERTH'
            LIMIT 1
        ) AS median_dwell_hours
    FROM mart.dashboard_current dc
    WHERE dc.is_liquid_cargo_vessel
      AND dc.arrival_at_utc IS NOT NULL
      AND dc.departure_at_utc IS NULL
      AND dc.draught IS NOT NULL AND dc.draught > 0
      AND NOT EXISTS (
          SELECT 1 FROM berth_assignment ba
          WHERE upper(btrim(ba.call_sign)) = upper(btrim(dc.callsgn))
            AND ba.status NOT IN ('CANCELLED', 'REJECTED')
      )
    ORDER BY dc.arrival_at_utc ASC
""")


async def _insert_requested(
    db: AsyncSession, *, berth_id: str, slot_no: int,
    callsgn: str, vessel_name: str | None, imo_no: str | None,
    chem_id: str, window_start: datetime, window_end: datetime,
    assignment_reason: str, rejected_candidates: dict,
    actual_berthing_at: datetime | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    db.add(BerthAssignment(
        berth_id=berth_id,
        slot_no=slot_no,
        call_sign=callsgn,
        imo_no=imo_no,
        vessel_name=vessel_name,
        cargo_chem_id=chem_id,
        planned_window=(window_start, window_end),
        status=STATUS_REQUESTED,
        assignment_reason=assignment_reason,
        rejected_candidates=rejected_candidates,
        # 이 배는 이미 VTS에 입항이 확인된 상태다(mart.dashboard_current.arrival_at_utc,
        # _QUERY_PENDING_ARRIVALS가 이미 이 조건으로 걸러냄) — window_start는 그 실측
        # 시각을 그대로 쓰고 있으므로(아래), 여기서도 같은 값을 실제 접안 확인 컬럼에
        # 넣는다(2026-08-20, "선박정보에서 입출항 시간 못 가져오냐"는 지적). window_end는
        # 실제 출항이 아니라 24시간 추정값이라 actual_departure_at에는 안 넣는다 —
        # 그건 scheduler.py::release_completed_berths가 VTS 출항 확인 시점에 채운다.
        actual_berthing_at=actual_berthing_at,
        created_at=now,
    ))
    await _clear_exclusion(db, call_sign=callsgn)
    await db.commit()


_EXCLUSION_DECISIONS = {
    OverallDecision.NO_ELIGIBLE_BERTH.name,
    OverallDecision.ALL_CANDIDATES_UNSAFE.name,
    OverallDecision.WEATHER_BLOCKED.name,
}


async def _clear_exclusion(db: AsyncSession, *, call_sign: str) -> None:
    """이 배가 이번 주기에 배정/대기열 등록으로 해소됐으니 남아 있던 경고를 지운다."""
    await db.execute(
        text("DELETE FROM scheduling_exclusion WHERE call_sign = :call_sign"),
        {"call_sign": call_sign},
    )


async def _upsert_exclusion(
    db: AsyncSession, *, row: dict, decision: OverallDecision,
    reason: str | None, window_start: datetime, window_end: datetime,
) -> None:
    """자동 추천이 안 된 사유를 배 1척당 최신 상태 1행으로 기록한다(§5.3 3번,
    /dashboard/alerts 연동). 이미 같은 배로 걸려 있던 행이 있으면 사유/시각만
    갱신한다 — 매 주기 새 행을 쌓으면 관제 화면에 같은 배가 중복 노출된다."""
    now = datetime.now(timezone.utc)
    existing = (
        await db.execute(
            text("SELECT id FROM scheduling_exclusion WHERE call_sign = :call_sign"),
            {"call_sign": row["callsgn"]},
        )
    ).first()
    if existing:
        await db.execute(
            text(
                "UPDATE scheduling_exclusion SET decision = :decision, reason = :reason, "
                "draught_m = :draught_m, window_start = :window_start, window_end = :window_end, "
                "updated_at = :now WHERE call_sign = :call_sign"
            ),
            {
                "decision": decision.name, "reason": reason, "draught_m": row["draught_m"],
                "window_start": window_start, "window_end": window_end, "now": now,
                "call_sign": row["callsgn"],
            },
        )
    else:
        db.add(SchedulingExclusion(
            call_sign=row["callsgn"], vessel_name=row["vessel_name"], imo_no=row["imo_no"],
            cargo_chem_id=row["chem_id"], decision=decision.name, reason=reason,
            draught_m=row["draught_m"], window_start=window_start, window_end=window_end,
            created_at=now, updated_at=now,
        ))


async def watch_arrivals() -> None:
    """입항허가 완료 + 아직 배정 없는 액체화물선을 순회하며 오케스트레이터 추천을
    자동 생성한다(status=REQUESTED — 확정 아님, §5.3)."""
    llm_client = get_llm_client()

    async with AsyncSessionFactory() as db:
        rows = (await db.execute(_QUERY_PENDING_ARRIVALS)).mappings().all()

    if not rows:
        # 대상이 아예 없으면(전부 배정됐거나 출항) 남아 있던 경고도 전부 해소된 것 —
        # scheduling_exclusion을 그대로 두면 이미 떠난 배 경고가 화면에 계속 남는다.
        async with AsyncSessionFactory() as db:
            await db.execute(text("DELETE FROM scheduling_exclusion"))
            await db.commit()
        logger.info("watch_arrivals: 대상 없음")
        return

    # 이번 주기에 더는 "입항허가 완료 + 미배정" 상태가 아닌 배(배정 완료 또는 출항)의
    # 경고는 자기치유적으로 정리한다 — watch_arrivals가 유일한 쓰기 경로이므로 여기서
    # 안 지우면 영영 안 지워진다.
    pending_callsigns = [row["callsgn"] for row in rows]
    async with AsyncSessionFactory() as db:
        await db.execute(
            text("DELETE FROM scheduling_exclusion WHERE call_sign <> ALL(CAST(:callsigns AS text[]))"),
            {"callsigns": pending_callsigns},
        )
        await db.commit()

    logger.info("watch_arrivals: %d척 처리 시작", len(rows))
    for raw_row in rows:
        if not raw_row["chem_id"]:
            logger.info("watch_arrivals: %s 화물 미식별(chem_id 없음) - 건너뜀", raw_row["callsgn"])
            continue

        # mart.dashboard_current.imo_no는 소스에 따라 정수로 올 때가 있다(실측
        # 확인, 2026-08-19) — BerthAssignment/AnchorageQueue/SchedulingExclusion의
        # imo_no는 전부 String 컬럼이라 그대로 넣으면 asyncpg가
        # "expected str, got int"로 거부하고 그 배는 물론 이후 배 전체 처리가
        # 중단된다(예외가 이 루프 밖으로 전파됨). 여기서 한 번만 정규화한다.
        row = dict(raw_row)
        if row["imo_no"] is not None:
            row["imo_no"] = str(row["imo_no"])

        # 계획기간(planned_window) — 여기가 틀리면 추천이 만들어지자마자 만료된다.
        #
        # 예전에는 [입항시각, 입항시각+24h] 였다. 그런데 이 쿼리가 뽑는 배는 전부
        # "아직 출항하지 않은" 배다(departure_at_utc IS NULL). 즉 5일 전에 들어와
        # 지금도 항내에 있는 배가 섞이는데, 그런 배는 창이 나흘 전에 끝나 버린다.
        # 그 결과 추천 123건 중 화면 필터(upper(planned_window) > now())를 통과하는
        # 것이 3건뿐이었고, 선석 배정현황이 늘 "0건"으로 보였다(2026-08-20 실측).
        #
        # 창의 끝은 "이 배가 언제 자리를 비우는가"다. 출항 예정 시각(ETD)이 원천에
        # 없으므로 그 선석의 실제 재항 이력 중앙값(mart.berth_dwell_stats, 실측
        # 29,607건)으로 잡는다. 다만 아직 항내에 있는 배는 그 중앙값을 이미 넘겼을
        # 수 있으므로, 최소한 지금부터 한 주기(MIN_FORWARD_HOURS)는 살아 있게 한다 —
        # "지금 자리를 쓰고 있다"는 사실 자체가 창이 아직 안 닫혔다는 뜻이다.
        now = datetime.now(timezone.utc)
        window_start = row["arrival_at_utc"] or now
        dwell_h = row.get("median_dwell_hours") or DEFAULT_WINDOW_HOURS
        window_end = max(
            window_start + timedelta(hours=float(dwell_h)),
            now + timedelta(hours=MIN_FORWARD_HOURS),
        )

        request = OrchestratorRequest(
            vessel=VesselSpec(draught_m=row["draught_m"], dwt_t=None, name_hint=row["vessel_name"]),
            cargo=CargoRef(chem_id=row["chem_id"]),
            window_start=window_start,
            window_end=window_end,
            assigned_wharf_name=None,  # 항상 탐색모드(§1.2, §5.1)
        )

        async with AsyncSessionFactory() as db:
            try:
                result = await orchestrate(db, neo4j_client.driver, llm_client, request)
            except Exception:
                logger.exception("watch_arrivals: %s 오케스트레이터 호출 실패", row["callsgn"])
                continue

            if result.overall_decision is OverallDecision.WAITING_ANCHORAGE:
                # 정박지는 잠금 대상이 아니므로 berth_assignment가 아니라 anchorage_queue에
                # 등록한다(§4.3, §5.3 "정박지 대기는 같은 승인 게이트를 타지 않는다").
                now = datetime.now(timezone.utc)
                db.add(AnchorageQueue(
                    call_sign=row["callsgn"], vessel_name=row["vessel_name"], imo_no=row["imo_no"],
                    cargo_chem_id=row["chem_id"], draught_m=row["draught_m"],
                    anchorage_id=result.anchorage_assignment.anchorage_id if result.anchorage_assignment else None,
                    entered_at=now, window_start=window_start, window_end=window_end,
                    status=STATUS_WAITING, assignment_reason=result.summary,
                    created_at=now,
                ))
                await _clear_exclusion(db, call_sign=row["callsgn"])
                await db.commit()
                logger.info("watch_arrivals: %s -> 정박지 대기열 등록", row["callsgn"])
                continue

            if result.overall_decision is not OverallDecision.APPROVED or not result.selected_berth:
                # NO_ELIGIBLE_BERTH/ALL_CANDIDATES_UNSAFE/WEATHER_BLOCKED는 berth_assignment를
                # 만들지 않고, 대신 scheduling_exclusion에 기록해 관제 경고 센터
                # (/dashboard/alerts)가 노출하게 한다(§5.3 3번).
                if result.overall_decision.name in _EXCLUSION_DECISIONS:
                    await _upsert_exclusion(
                        db, row=row, decision=result.overall_decision,
                        reason=result.summary, window_start=window_start, window_end=window_end,
                    )
                    await db.commit()
                logger.info(
                    "watch_arrivals: %s -> %s (배정 대상 아님)",
                    row["callsgn"], result.overall_decision.value,
                )
                continue

            slot_no = await find_free_slot(
                db, berth_id=result.selected_berth.berth_id,
                window_start=window_start, window_end=window_end,
            )
            if slot_no is None:
                # 추천 계산 시점과 INSERT 시점 사이의 경합(드묾) — 다음 주기에 재시도된다.
                logger.warning(
                    "watch_arrivals: %s 추천 선석 '%s' 방금 만석 - 다음 주기 재시도",
                    row["callsgn"], result.selected_berth.wharf_name,
                )
                continue

            await _insert_requested(
                db,
                berth_id=result.selected_berth.berth_id,
                slot_no=slot_no,
                callsgn=row["callsgn"],
                vessel_name=row["vessel_name"],
                imo_no=row["imo_no"],
                chem_id=row["chem_id"],
                window_start=window_start,
                window_end=window_end,
                assignment_reason=result.summary,
                rejected_candidates=result.decision_detail(),
                actual_berthing_at=row["arrival_at_utc"],
            )
            logger.info(
                "watch_arrivals: %s -> '%s' 슬롯 %d 추천(REQUESTED)",
                row["callsgn"], result.selected_berth.wharf_name, slot_no,
            )
