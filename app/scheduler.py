"""출항 확인 → berth_assignment 자동 해제 (10분 주기).

08_스케줄링_전면재설계_자동배정_설계문서.md §5.5 — 07_입항승인_선석확정_설계문서.md
§5.4 설계를 변경 없이 구현한다. 출항 이벤트를 호출부호로 대조해, 활성 상태
(REQUESTED/APPROVED/SCHEDULED/BERTHED)인 berth_assignment의 planned_window
상한을 닫고 status='COMPLETED'로 바꾼다.

이 잡은 "자원을 새로 점유하지 않는 반납 동작"이라 §5.3의 관제사 승인 게이트와
무관하게 그대로 자동이다 — 승인이 필요한 건 선석을 새로 잠그는 결정뿐이다.

슬롯이 하나 비워질 때마다 anchorage_promoter(§5.4)를 그 자리에서 바로 호출한다 —
정박지 대기열 폴링을 별도로 돌리지 않고, "빈자리가 생긴 이벤트"에 반응하는 구조다
(설계문서 §5.4 시퀀스 다이어그램 그대로).

(2026-08-20) 출항 판정 소스에 portmis_vessel을 추가했다 — PORT-MIS는 선사가
공식으로 신고하는 행정 기록이라 VTS(upa_port_call) 위치 관측보다 신뢰도가
높고, callsgn만으로 바로 대조되어(선석명 정규화 불필요) upa_port_call 경로가
안고 있던 facility_alias 매칭 문제(완전일치 5.7%) 자체가 없다. 두 소스 다
확인해 더 이른 쪽이 아니라 "PORT-MIS가 있으면 그걸 쓰고, 없으면 VTS로
보충"한다 — PORT-MIS 쪽이 공식 기록이라 우선한다.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from app.database import AsyncSessionFactory
from app.jobs.anchorage_promoter import promote_anchorage_queue
from app.jobs.arrival_watcher import watch_arrivals
from app.llm.factory import get_llm_client
from app.models.berth_assignment import ACTIVE_STATUSES, STATUS_COMPLETED
from app.neo4j_client import neo4j_client

logger = logging.getLogger("scheduler")

JOB_INTERVAL_MINUTES = 10  # arrival_watcher와 동일 주기로 통일(§5.1, §5.5)

# callsgn(호출부호)을 정규화해 대조한다(occupancy.py와 동일 upper(btrim(...)) 관행).
#
# PORT-MIS(공식 신고)를 우선하고, 없는 배만 VTS 관측(upa_port_call)으로 보충한다.
# VTS 경로는 옛날에 facility_name -> wharf_name을 mart.facility_alias로 맞춰
# berth_id까지 일치해야 했지만(완전일치 대조 시 5.7%만 매칭되는 문제가 있었음,
# occupancy.py 주석 참고), 지금은 callsgn 단독 매칭이다 — 이 배정에 걸린 배가
# "포트 어딘가에서" 공식으로 출항 확인됐으면 그 자리는 비워야 한다는 뜻이라,
# 선석명까지 다시 맞출 필요가 없다(오히려 그 매칭 실패가 해제를 막는 원인이었다).
_QUERY_DEPARTED_ACTIVE_ASSIGNMENTS = text("""
    WITH departed_portmis AS (
        SELECT DISTINCT ON (upper(btrim(pv.callsgn)))
            upper(btrim(pv.callsgn)) AS callsgn_norm, pv.departure_at_utc
        FROM portmis_vessel pv
        WHERE pv.departure_at_utc IS NOT NULL
          AND nullif(btrim(pv.callsgn), '') IS NOT NULL
        ORDER BY upper(btrim(pv.callsgn)), pv.departure_at_utc DESC
    ),
    departed_vts AS (
        SELECT DISTINCT ON (upper(btrim(pc.callsgn)))
            upper(btrim(pc.callsgn)) AS callsgn_norm, pc.departure_at_utc
        FROM upa_port_call pc
        WHERE pc.departure_at_utc IS NOT NULL
          AND nullif(btrim(pc.callsgn), '') IS NOT NULL
        ORDER BY upper(btrim(pc.callsgn)), pc.departure_at_utc DESC
    ),
    departed AS (
        SELECT COALESCE(pm.callsgn_norm, vts.callsgn_norm) AS callsgn_norm,
               COALESCE(pm.departure_at_utc, vts.departure_at_utc) AS departure_at_utc
        FROM departed_portmis pm
        FULL OUTER JOIN departed_vts vts ON vts.callsgn_norm = pm.callsgn_norm
    )
    SELECT ba.id, ba.berth_id, ba.slot_no, d.departure_at_utc
    FROM berth_assignment ba
    JOIN departed d ON upper(btrim(ba.call_sign)) = d.callsgn_norm
    WHERE ba.status = ANY(CAST(:active_statuses AS text[]))
      -- 역전 방지 가드(07 문서 §5.4) — 출항시각이 예약 생성(하한)보다 이르면 건너뛴다.
      AND d.departure_at_utc > lower(ba.planned_window)
""")

_UPDATE_COMPLETE_ASSIGNMENT = text("""
    UPDATE berth_assignment
    SET status = :status,
        actual_departure_at = :departure_at_utc,
        planned_window = tstzrange(lower(planned_window), :departure_at_utc, '[)'),
        updated_at = now()
    WHERE id = :id
""")


async def release_completed_berths() -> None:
    """출항이 확인된 활성 berth_assignment를 COMPLETED로 닫고, 빈 슬롯마다 승격을 시도한다."""
    async with AsyncSessionFactory() as db:
        rows = (
            await db.execute(
                _QUERY_DEPARTED_ACTIVE_ASSIGNMENTS,
                {"active_statuses": list(ACTIVE_STATUSES)},
            )
        ).mappings().all()

        if not rows:
            logger.info("release_completed_berths: 닫을 대상 없음")
            return

        freed: list[tuple[str, int]] = []
        for row in rows:
            await db.execute(
                _UPDATE_COMPLETE_ASSIGNMENT,
                {
                    "id": row["id"],
                    "status": STATUS_COMPLETED,
                    "departure_at_utc": row["departure_at_utc"],
                },
            )
            freed.append((row["berth_id"], row["slot_no"]))
        await db.commit()
        logger.info("release_completed_berths: %d건 COMPLETED 처리", len(freed))

    llm_client = get_llm_client()
    for berth_id, slot_no in freed:
        try:
            await promote_anchorage_queue(
                neo4j_driver=neo4j_client.driver, llm_client=llm_client,
                berth_id=berth_id, slot_no=slot_no,
            )
        except Exception:
            logger.exception("anchorage_promoter 실패 (berth_id=%s, slot_no=%s)", berth_id, slot_no)


def create_scheduler() -> AsyncIOScheduler:
    """§5.1·§5.5 — arrival_watcher(자동 추천)와 release_completed_berths(자동 해제)를
    같은 10분 주기로 등록한다(설계문서 "왜 폴링 주기를 통일하는가" 참고 — 출항 해제 →
    빈자리 승격 → 신규 입항 추천이 이어지는 흐름에서 서로 다른 주기면 불필요한 지연이
    생긴다). anchorage_promoter는 별도 잡이 아니라 release_completed_berths가 슬롯을
    비울 때마다 그 안에서 직접 호출한다(§5.4, 이벤트 구동).
    """
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        release_completed_berths,
        trigger=IntervalTrigger(minutes=JOB_INTERVAL_MINUTES),
        id="release_completed_berths",
        replace_existing=True,
        max_instances=1,  # 이전 실행이 안 끝났으면 겹쳐 돌리지 않는다
    )
    scheduler.add_job(
        watch_arrivals,
        trigger=IntervalTrigger(minutes=JOB_INTERVAL_MINUTES),
        id="watch_arrivals",
        replace_existing=True,
        max_instances=1,
    )
    return scheduler


if __name__ == "__main__":
    # 단독 실행(디버깅용) — 평소엔 main.py의 lifespan이 create_scheduler()를 돌린다.
    logging.basicConfig(level=logging.INFO)
    asyncio.run(release_completed_berths())
