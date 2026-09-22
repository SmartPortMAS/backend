"""백엔드 백그라운드 잡 등록.

[2026-09-21 전면 개편] 잡이 둘에서 하나로 줄었다.

없앤 것: `release_completed_berths`(출항 확인 → `berth_assignment` 자동 해제)와
그 안에서 이벤트로 불리던 `anchorage_promoter`(빈 슬롯 → 정박지 대기열 승격).

둘 다 **선석을 자원으로 잠갔다 푸는 동작**이다. 우리는 선석을 잠그지 않으므로
풀 것도 없다(9/17 회의 §1, 방향 C). 특히 `anchorage_promoter` 는 빈 슬롯이 생기면
대기 선박을 그 선석에 **배정**했다(`find_free_slot` → `BerthAssignment` 생성) —
우리가 할 수 없는 일이다.

출항은 이제 판정에서 이렇게 다룬다. `arrival_watcher` 의 검증 대상은 "AIS 가
지금 붙어 있다고 말하는 배"를 포함하므로, 배가 뜨면 **다음 주기에 대상에서
저절로 빠진다.** 닫아야 할 행이 없다 — `assessment_history` 는 시각이 찍힌
판정 기록이지 살아 있는 예약이 아니기 때문이다.

남은 잡은 하나다.
    watch_arrivals — 검증 대상을 순회하며 판정을 assessment_history 에 남긴다.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.jobs.arrival_watcher import watch_arrivals

logger = logging.getLogger("scheduler")

JOB_INTERVAL_MINUTES = 10
# UPA 선박위치 게시 지연 실측(2026-09-21, 표본 5): 3.9~12.7분. 10분 주기면
# 매 주기 최소 한 번은 새 위치를 본다. 다만 지연이 일정하지 않으므로, 신선도가
# 중요한 판단은 주기를 가정하지 말고 received_at_utc 를 직접 읽어야 한다.


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        watch_arrivals,
        trigger=IntervalTrigger(minutes=JOB_INTERVAL_MINUTES),
        id="watch_arrivals",
        replace_existing=True,
        max_instances=1,  # 이전 실행이 안 끝났으면 겹쳐 돌리지 않는다
        coalesce=True,
    )
    return scheduler


if __name__ == "__main__":
    # 단독 실행(디버깅용) — 평소엔 main.py 의 lifespan 이 create_scheduler()를 돌린다.
    logging.basicConfig(level=logging.INFO)
    asyncio.run(watch_arrivals())
