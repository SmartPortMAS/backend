"""선석 점유 여부 조회 — berth_assignment(우리 시스템 배정 기록) 기준.

(2026-08-19 결정) 점유 판정은 upa_port_call(VTS 실측)이 아니라 berth_assignment만
본다. 이 스케줄링 에이전트가 선석을 직접 배정하는 주체이므로, 점유 여부도 그
배정 기록 스스로가 기준이어야 한다 — VTS 데이터를 섞으면 "우리가 배정한 게
아닌데도 점유"라는 모순이 생기고, 애초에 스케줄링 에이전트를 두는 이유(우리
기준으로 직접 배정)와도 맞지 않는다. 예전에 VTS(upa_port_call) 기반 점유 조회를
같이 썼던 적이 있는데, 실측해보니 출항 미기록 상태로 방치된 "유령 재항" 기록이
3,910건(최고 8개월분)이나 있어 데이터 신뢰도 자체가 이 판단에 못 미쳤다.
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# 슬롯 단위 점유 확인 (2026-08-19,
# 08_스케줄링_전면재설계_자동배정_설계문서.md §4.2, §5.2 6번 게이트)
#
# 07 문서 §5.1이 이미 SQL을 설계해 뒀지만 실제 구현이 없었다(2026-08-19 확인 —
# 이 파일 전체에 slot_no를 다루는 코드가 이거 추가 전까지 없었음). berth_assignment
# EXCLUDE 제약이 (berth_id, slot_no, planned_window)로 걸려 있으므로, 새 추천을
# INSERT하려면 어느 slot_no가 비어 있는지 먼저 알아야 한다 — 그렇지 않으면 슬롯
# 2개 이상인 선석(69개 중 45%, 07 문서 §4.1.1)에서 실제로는 자리가 남았는데도
# slot_no=1 하나만 쓰다가 EXCLUDE 위반으로 잘못 막히거나, 반대로 다른 배와 같은
# slot_no를 노려 위반이 나는 것도 못 걸러낸다.
# ---------------------------------------------------------------------------

_QUERY_MAX_CONCURRENT_VESSELS = text("""
    SELECT berth_vessel_count FROM upa_berth_facility WHERE wharf_name = :berth_id
""")

_QUERY_FIND_FREE_SLOT = text("""
    SELECT gs.slot_no
    FROM generate_series(1, :max_concurrent_vessels) AS gs(slot_no)
    WHERE NOT EXISTS (
        SELECT 1 FROM berth_assignment ba
        WHERE ba.berth_id = :berth_id AND ba.slot_no = gs.slot_no
          AND ba.status IN ('REQUESTED', 'APPROVED', 'SCHEDULED', 'BERTHED')
          AND ba.planned_window && tstzrange(:window_start, :window_end, '[)')
          -- find_overlapping_reservations와 동일한 이유로 자기 예약은 점유로
          -- 세지 않는다(2026-08-21 실측 재현 — 1슬롯 선석에서 이미 자기 배가
          -- 그 슬롯을 쓰고 있는 상태로 assess-and-commit을 다시 부르면
          -- "만석"으로 오판정돼 재확정이 막혔다).
          AND (
              CAST(:exclude_call_sign AS text) IS NULL
              OR upper(btrim(ba.call_sign)) <> upper(btrim(CAST(:exclude_call_sign AS text)))
          )
    )
    ORDER BY gs.slot_no
    LIMIT 1
""")


_QUERY_OVERLAPPING_RESERVATIONS = text("""
    SELECT berth_id, call_sign AS vessel_name,
           lower(planned_window) AS arrival_at_utc, upper(planned_window) AS departure_at_utc
    FROM berth_assignment
    WHERE berth_id = ANY(CAST(:berth_ids AS text[]))
      AND status IN ('REQUESTED', 'APPROVED', 'SCHEDULED', 'BERTHED')
      AND planned_window && tstzrange(:window_start, :window_end, '[)')
      -- 자기 자신이 잡아 둔 예약은 점유로 세지 않는다.
      --
      -- arrival_watcher 가 A 배에 B 선석을 추천해 REQUESTED 행을 만든 뒤,
      -- 관제사가 콘솔에서 A 를 다시 판정하면 B 가 "점유 중(우리 시스템 배정
      -- 기록 있음)"으로 나왔다. 자기 예약이 자기를 막은 것이라, 추천을 받은
      -- 배는 승인 버튼이 뜨는 조건('승인가능')에 영원히 도달하지 못했다
      -- (2026-08-21 실측 — 승인 대기 37건 전부 승인 불가 상태였다).
      AND (
          CAST(:exclude_call_sign AS text) IS NULL
          OR upper(btrim(call_sign)) <> upper(btrim(CAST(:exclude_call_sign AS text)))
      )
""")


async def find_overlapping_reservations(
    db: AsyncSession, *, berth_ids: list[str], window_start: datetime, window_end: datetime,
    exclude_call_sign: str | None = None,
) -> dict[str, list[dict]]:
    """우리 시스템이 이미 REQUESTED~BERTHED로 잡아 둔 예약(berth_assignment) 중
    요청 시간대와 겹치는 것 — 점유 판정의 유일한 근거(모듈 docstring 참고).
    키는 berth_id다(wharf_name이 아니다 — berth_assignment.berth_id는
    upa_berth_facility.wharf_name과 같은 값).
    """
    if not berth_ids:
        return {}
    rows = (
        await db.execute(
            _QUERY_OVERLAPPING_RESERVATIONS,
            {
                "berth_ids": berth_ids, "window_start": window_start,
                "window_end": window_end, "exclude_call_sign": exclude_call_sign,
            },
        )
    ).mappings().all()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["berth_id"], []).append(
            {
                "vessel_name": row["vessel_name"],
                "arrival_at_utc": row["arrival_at_utc"],
                "departure_at_utc": row["departure_at_utc"],
                "source_facility_name": "(자동배정 예약)",
            }
        )
    return grouped


async def find_free_slot(
    db: AsyncSession, *, berth_id: str, window_start: datetime, window_end: datetime,
    exclude_call_sign: str | None = None,
) -> int | None:
    """berth_id의 슬롯(1..max_concurrent_vessels) 중 요청 시간대와 안 겹치는 가장
    작은 slot_no를 찾는다. 전부 찼으면(만석) None.

    upa_berth_facility.berth_vessel_count가 없는 선석(결측 또는 미매칭)은 1로
    간주한다 — 07 문서 §4.1.1의 기본값과 동일.

    exclude_call_sign을 넘기면 그 배 자신의 기존 예약은 점유로 세지 않는다
    (find_overlapping_reservations와 동일한 목적 — assess-and-commit이 이미
    예약이 있는 배를 같은 시간대로 재확정할 때 자기 자신에게 막히지 않도록).
    """
    max_row = (
        await db.execute(_QUERY_MAX_CONCURRENT_VESSELS, {"berth_id": berth_id})
    ).first()
    # berth_vessel_count는 double precision이라 generate_series(integer, ...)에 그대로
    # 못 넘긴다(타입 불일치) — int로 캐스팅해서 넘긴다.
    max_concurrent = int(max_row[0]) if max_row and max_row[0] else 1

    row = (
        await db.execute(
            _QUERY_FIND_FREE_SLOT,
            {
                "berth_id": berth_id,
                "max_concurrent_vessels": max_concurrent,
                "window_start": window_start,
                "window_end": window_end,
                "exclude_call_sign": exclude_call_sign,
            },
        )
    ).first()
    return row[0] if row else None
