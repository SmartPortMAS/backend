"""PostgreSQL upa_port_call 기반 선석 점유 여부 조회.

upa_port_call은 data-pipeline UPA 로더(함현우 담당)가 auto_create로 만든 테이블이라
backend Alembic이 소유하지 않는다 (schema ownership 원칙: 여러 곳이 쓰는 테이블만
backend가 소유, UPA 6종은 기존 방식 유지). 그래서 ORM 모델 없이 raw SQL로 읽기만
한다.

한 접안 이벤트(port_call_id)가 입항/접안/출항/이선 등 여러 행으로 중복 기록되므로
DISTINCT ON (port_call_id)으로 대표 행 하나만 남긴다. departure_at_utc가 NULL인
경우(아직 출항 미기록) 요청 구간과 항상 겹치는 것으로 보수적으로 판단한다.
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_QUERY_OVERLAPPING_PORT_CALLS = text("""
    SELECT DISTINCT ON (port_call_id)
        port_call_id, facility_name, vessel_name, arrival_at_utc, departure_at_utc
    FROM upa_port_call
    WHERE facility_name = ANY(:wharf_names)
      AND arrival_at_utc IS NOT NULL
      AND arrival_at_utc < :window_end
      AND (departure_at_utc IS NULL OR departure_at_utc > :window_start)
    ORDER BY port_call_id, arrival_at_utc
""")


async def find_overlapping_port_calls(
    db: AsyncSession,
    *,
    wharf_names: list[str],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, list[dict]]:
    """요청 시간대와 겹치는 입출항 기록을 wharf_name(facility_name) 기준으로 묶어 반환.

    Returns:
        { wharf_name: [{"vessel_name", "arrival_at_utc", "departure_at_utc"}, ...] }
        겹치는 기록이 없는 wharf_name은 결과 딕셔너리에 아예 나타나지 않는다(= 여유).
    """
    if not wharf_names:
        return {}

    result = await db.execute(
        _QUERY_OVERLAPPING_PORT_CALLS,
        {"wharf_names": wharf_names, "window_start": window_start, "window_end": window_end},
    )

    grouped: dict[str, list[dict]] = {}
    for row in result.mappings():
        grouped.setdefault(row["facility_name"], []).append(
            {
                "vessel_name": row["vessel_name"],
                "arrival_at_utc": row["arrival_at_utc"],
                "departure_at_utc": row["departure_at_utc"],
            }
        )
    return grouped
