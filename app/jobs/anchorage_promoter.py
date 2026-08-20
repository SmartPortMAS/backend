"""정박지 대기 → 빈자리 자동 승격 (이벤트 구동, §5.4).

scheduler.py의 release_completed_berths()가 슬롯을 하나 비울 때마다 호출된다.
별도 폴링 잡이 아니다 — "빈자리가 생긴 이벤트"에 반응하는 구조(설계문서 §5.4
시퀀스 다이어그램).

오케스트레이터의 검증모드(assigned_wharf_name)를 재사용해 "이 선석이 지금
이 화물에 안전한지"(기상·안전관제·슬롯)를 재확인한다 — 다만 검증모드는
화물 카테고리를 확인하지 않으므로(scheduling/service.py::build_candidate_for_wharf_name
주석), 호출 전에 이 선석이 취급하는 카테고리로 대기열을 먼저 걸러야 한다.
"""

import logging
from datetime import datetime, timedelta, timezone

from neo4j import AsyncDriver
from sqlalchemy import text

from app.agents.orchestrator.schemas import OrchestratorRequest, OverallDecision
from app.agents.orchestrator.service import orchestrate
from app.agents.safety.schemas import CargoRef
from app.agents.scheduling.graph_queries import get_berth_categories, get_chemical_category
from app.agents.scheduling.occupancy import find_free_slot
from app.agents.scheduling.schemas import VesselSpec
from app.database import AsyncSessionFactory
from app.llm.base import LLMClient
from app.models.anchorage_queue import STATUS_PROMOTED, STATUS_WAITING, AnchorageQueue
from app.models.berth_assignment import STATUS_REQUESTED, BerthAssignment

logger = logging.getLogger("anchorage_promoter")

DEFAULT_WINDOW_HOURS = 24

_QUERY_WAITING_QUEUE = text("""
    SELECT id, call_sign, vessel_name, imo_no, cargo_chem_id, dwt_t, draught_m,
           window_start, window_end, entered_at
    FROM anchorage_queue
    WHERE status = :status
    ORDER BY entered_at ASC
""")


async def promote_anchorage_queue(
    *, neo4j_driver: AsyncDriver, llm_client: LLMClient, berth_id: str, slot_no: int,
) -> None:
    async with AsyncSessionFactory() as db:
        # berth_id는 이제 upa_berth_facility.wharf_name과 같은 값이다(FK 대상) —
        # 별도 조회 없이 그대로 wharf_name으로 쓴다.
        wharf_name = berth_id

        categories = await get_berth_categories(neo4j_driver, berth_id=berth_id)
        if not categories:
            logger.info("promote_anchorage_queue: '%s'가 HANDLES하는 카테고리 없음 - 승격 대상 아님", wharf_name)
            return

        queue_rows = (await db.execute(_QUERY_WAITING_QUEUE, {"status": STATUS_WAITING})).mappings().all()
        if not queue_rows:
            logger.info("promote_anchorage_queue: 대기열 비어 있음")
            return

        for q in queue_rows:
            if not q["cargo_chem_id"]:
                continue
            category = await get_chemical_category(neo4j_driver, q["cargo_chem_id"])
            if category not in categories:
                continue  # 이 선석이 취급 안 하는 화물 — FCFS 순서를 건너뛰고 다음 대기 선박으로

            window_start = q["window_start"] or datetime.now(timezone.utc)
            window_end = q["window_end"] or (window_start + timedelta(hours=DEFAULT_WINDOW_HOURS))

            request = OrchestratorRequest(
                vessel=VesselSpec(draught_m=q["draught_m"], dwt_t=q["dwt_t"], name_hint=q["vessel_name"]),
                cargo=CargoRef(chem_id=q["cargo_chem_id"]),
                window_start=window_start,
                window_end=window_end,
                assigned_wharf_name=wharf_name,  # 검증모드 — 이 선석 하나만 재확인(§5.4)
            )

            try:
                result = await orchestrate(db, neo4j_driver, llm_client, request)
            except Exception:
                logger.exception("promote_anchorage_queue: %s 오케스트레이터 호출 실패", q["call_sign"])
                continue

            if result.overall_decision is not OverallDecision.APPROVED or not result.selected_berth:
                logger.info(
                    "promote_anchorage_queue: %s -> %s (재검증 실패, 대기열 유지, 다음 대기 선박 시도)",
                    q["call_sign"], result.overall_decision.value,
                )
                continue

            slot = await find_free_slot(
                db, berth_id=result.selected_berth.berth_id,
                window_start=window_start, window_end=window_end,
            )
            if slot is None:
                logger.warning(
                    "promote_anchorage_queue: %s 추천 선석 '%s' 방금 만석 - 다음 대기 선박 시도",
                    q["call_sign"], result.selected_berth.wharf_name,
                )
                continue

            now = datetime.now(timezone.utc)
            new_assignment = BerthAssignment(
                berth_id=result.selected_berth.berth_id,
                slot_no=slot,
                call_sign=q["call_sign"],
                imo_no=q["imo_no"],
                vessel_name=q["vessel_name"],
                cargo_chem_id=q["cargo_chem_id"],
                planned_window=(window_start, window_end),
                status=STATUS_REQUESTED,
                assignment_reason=result.summary,
                rejected_candidates={**result.decision_detail(), "promoted_from_anchorage": True},
                created_at=now,
            )
            db.add(new_assignment)
            await db.flush()

            await db.execute(
                text(
                    "UPDATE anchorage_queue SET status = :status, "
                    "promoted_berth_assignment_id = :ba_id, updated_at = :now WHERE id = :id"
                ),
                {"status": STATUS_PROMOTED, "ba_id": new_assignment.id, "now": now, "id": q["id"]},
            )
            await db.commit()
            logger.info(
                "promote_anchorage_queue: %s 대기열 -> '%s' 슬롯 %d 추천(REQUESTED)",
                q["call_sign"], result.selected_berth.wharf_name, slot,
            )
            return  # 이 빈자리 하나에는 한 척만 승격한다 — 나머지 빈 슬롯은 다음 release 이벤트가 처리

        logger.info("promote_anchorage_queue: '%s' 카테고리에 맞는 대기 선박 없음", wharf_name)
