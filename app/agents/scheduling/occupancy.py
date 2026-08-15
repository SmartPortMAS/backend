"""PostgreSQL upa_port_call 기반 선석 점유 여부 조회.

upa_port_call은 data-pipeline UPA 로더(함현우 담당)가 auto_create로 만든 테이블이라
backend Alembic이 소유하지 않는다 (schema ownership 원칙: 여러 곳이 쓰는 테이블만
backend가 소유, UPA 6종은 기존 방식 유지). 그래서 ORM 모델 없이 raw SQL로 읽기만
한다.

한 접안 이벤트(port_call_id)가 입항/접안/출항/이선 등 여러 행으로 중복 기록되므로
DISTINCT ON (port_call_id)으로 대표 행 하나만 남긴다. departure_at_utc가 NULL인
경우(아직 출항 미기록) 요청 구간과 항상 겹치는 것으로 보수적으로 판단한다.

[선석 이름은 반드시 mart.facility_alias 를 거친다]
upa_port_call.facility_name 은 VTS 운항관제 원문 표기이고, 호출측이 넘기는
wharf_names 는 Neo4j Berth 의 마스터 표기다. 두 어휘가 서로 다르다 —
    VTS 원문 : 'S-OIL1부두' · 'SK1부두 11' · 'OTK부두'
    마스터   : 'S-Oil 1부두' · 'SK1부두'    · 'OTK1부두'
예전에는 이 둘을 문자열 완전일치로 비교해서 액체화물 전용부두가 통째로 안 붙었다.
실측(2026-08-15): 완전일치는 행 기준 706/12,280 = 5.7%.

증상이 조용해서 더 나빴다 — 겹치는 기록이 0건이면 호출측(service.py)이
OccupancyStatus.AVAILABLE 로 판단하므로, **배가 11척 붙어 있는 OTK1부두가
'여유'로 1순위 추천**됐다. 게다가 OCCUPIED 가 아니면 resolve_berth_assignment()
가 대체 선석·정박지 탐색을 건너뛰어 온산 MVP 3단계 배정이 발동하지 않았다.

같은 저장소의 다른 소비자(dashboard.py, safety_index.py, scheduling/service.py)는
전부 이 사전을 거치고 있었고 여기만 빠져 있었다.
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_QUERY_OVERLAPPING_PORT_CALLS = text("""
    SELECT DISTINCT ON (pc.port_call_id)
        pc.port_call_id, fa.wharf_name, pc.facility_name AS source_facility_name,
        pc.vessel_name, pc.arrival_at_utc, pc.departure_at_utc
    FROM upa_port_call pc
    JOIN mart.facility_alias fa
      ON fa.source_name = pc.facility_name AND fa.facility_type = 'BERTH'
    WHERE fa.wharf_name = ANY(CAST(:wharf_names AS text[]))
      AND pc.arrival_at_utc IS NOT NULL
      AND pc.arrival_at_utc < :window_end
      AND (pc.departure_at_utc IS NULL OR pc.departure_at_utc > :window_start)
    ORDER BY pc.port_call_id, pc.arrival_at_utc
""")


async def find_overlapping_port_calls(
    db: AsyncSession,
    *,
    wharf_names: list[str],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, list[dict]]:
    """요청 시간대와 겹치는 입출항 기록을 wharf_name(마스터 표기) 기준으로 묶어 반환.

    Returns:
        { wharf_name: [{"vessel_name", "arrival_at_utc", "departure_at_utc"}, ...] }
        겹치는 기록이 없는 wharf_name은 결과 딕셔너리에 아예 나타나지 않는다(= 여유).

    키는 호출측이 넘긴 wharf_names 와 같은 어휘(마스터)다 — VTS 원문이 아니다.
    호출측이 `grouped.get(berth.wharf_name)` 으로 바로 찾을 수 있어야 하기 때문이다.
    """
    if not wharf_names:
        return {}

    result = await db.execute(
        _QUERY_OVERLAPPING_PORT_CALLS,
        {"wharf_names": wharf_names, "window_start": window_start, "window_end": window_end},
    )

    grouped: dict[str, list[dict]] = {}
    for row in result.mappings():
        grouped.setdefault(row["wharf_name"], []).append(
            {
                "vessel_name": row["vessel_name"],
                "arrival_at_utc": row["arrival_at_utc"],
                "departure_at_utc": row["departure_at_utc"],
                # 어느 VTS 표기에서 왔는지 남긴다 — 사전이 틀렸을 때 추적할 단서
                "source_facility_name": row["source_facility_name"],
            }
        )
    return grouped
