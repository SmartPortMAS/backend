"""선석 점유 조회 — **AIS 실측 접안** 기준.

[2026-09-21 전면 개편] 점유의 근거를 우리 표에서 현실로 옮겼다.

이전 판은 `berth_assignment`(우리가 만든 예약)만 봤다. 그 전제는 "이 에이전트가
선석을 직접 배정하는 주체이므로 점유도 그 배정 기록 스스로가 기준"이었다. 우리는
배정 주체가 아니다(9/17 회의 §1, 방향 C). 그러면 남는 질문은 하나다 —

    "지금 그 선석에 배가 실제로 붙어 있는가"

그 답은 `mart.vessel_presence` 에 이미 있다. 뷰가 선박 위치로 선석·정박지를
판정한다 — 신고 선석 1km 안이면 '신고+위치', 가장 가까운 선석 300m 안이면
'위치', 좌표가 없는 부이 등은 '신고'(mart_views.sql 2-1절).

[2026-09-22] `mart.berth_occupancy_live` 에서 옮겼다.
  같은 질문에 뷰가 둘이라 답이 갈렸다. 실측(같은 스냅샷):
      vessel_presence      접안 14척
      berth_occupancy_live 접안  0척
  후자는 now() 기준 30분 안의 위치만 PRESENT 로 봤는데, 그때 최신 위치가
  6시간 33분 전이라 전부 NO_SIGNAL 로 떨어졌다 — 수집이 잠깐만 밀려도 점유가
  통째로 0 이 되고, 그러면 이 에이전트는 "모든 선석이 비었다"고 답한다.

[왜 upa_port_call(VTS 이력)이 아닌가]
  2026-08-19 에 VTS 를 점유 근거에서 뺀 결정은 그대로 유효하다. 그건 **사후
  이력**이고 출항 미기록 '유령 재항'이 3,910건(최고 8개월분) 있었다. 여기서
  쓰는 건 사후 이력이 아니라 **실시간 위치**라 그 문제가 구조적으로 없다.

[신선도]
  판정은 최신 스냅샷 기준이라 수집이 밀려도 "마지막으로 본 상태"가 남는다.
  낡은 정도는 `quality_flag`(OK/DEGRADED/STALE/NO_SIGNAL)와 `position_age_min`
  으로 함께 돌려준다 — 호출부가 그 값을 보고 판단하게 두고, 여기서 조용히
  버리지 않는다. 신호가 끊긴 배를 지우면 자리가 비었다고 잘못 말하게 된다.

[이 값의 무게]
  점유는 **판정 등급을 바꾸지 않는다.** 오경보 백테스트 S2 가 점유 초과를
  정보로 강등했다 — PORT-MIS 출항 시각이 예정값이라 동시 계류를 과대 계산한다
  (2026-09-21 실측 7/125 = 5.6%). 관제사에게 "지금 이 선석엔 이 배들이 붙어
  있습니다"를 보여주는 **관측**이지, 부적합의 근거가 아니다.
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_QUERY_MAX_CONCURRENT_VESSELS = text("""
    SELECT berth_vessel_count FROM upa_berth_facility WHERE wharf_name = :berth_id
""")


# vp.berth_name 은 이미 wharf 테이블의 정본 표기다(뷰가 wharf 를 직접 참조한다).
# mart.facility_alias 를 거치면 안 된다 — 별칭 사전은 wharf_name 당 행이 여럿이라
# 조인이 척수를 뻥튀기한다(실측 오류: 2부두 실제 3척 -> 별칭 조인 시 9척).
#
# 뷰는 배 1척을 **한 구역 하나**에만 귀속시킨다(presence_zone). 그래서 반경이
# 이웃 부두와 겹쳐도(실측 최단 간격: 용잠1/2 0m, 3/4부두 150m, UTK신항/한진신항
# 190m) 중복 계수되지 않는다.
_QUERY_LIVE_OCCUPANTS = text("""
    SELECT vp.berth_name AS wharf_name, vp.callsgn, vp.vessel_name,
           vp.berth_dist_m AS distance_m, vp.berth_basis,
           vp.received_at_utc, vp.quality_flag, vp.position_age_min
    FROM mart.vessel_presence vp
    WHERE vp.presence_zone = 'BERTH'
      AND vp.berth_name = ANY(CAST(:berth_ids AS text[]))
      AND (
          CAST(:exclude_call_sign AS text) IS NULL
          OR upper(btrim(vp.callsgn)) <> upper(btrim(CAST(:exclude_call_sign AS text)))
      )
    ORDER BY vp.berth_name, vp.berth_dist_m NULLS LAST
""")


def _basis_label(row) -> str:
    """점유 판정 근거 한 줄. 거리·신선도는 있을 때만 붙인다."""
    parts = [f"위치 판정 {row['berth_basis'] or '근거 미상'}"]
    if row["distance_m"] is not None:
        parts.append(f"{int(row['distance_m'])}m")
    if row["quality_flag"] and row["quality_flag"] != "OK":
        parts.append(f"{row['quality_flag']} {row['position_age_min']}분 전")
    return "(" + ", ".join(parts) + ")"


async def find_overlapping_reservations(
    db: AsyncSession, *, berth_ids: list[str], window_start: datetime, window_end: datetime,
    exclude_call_sign: str | None = None,
) -> dict[str, list[dict]]:
    """지금 그 선석에 **실제로 붙어 있는 배**. 키는 wharf_name.

    이름에 'reservations'가 남아 있지만 예약이 아니라 관측이다 — 호출부
    (scheduling/service.py 3곳)를 건드리지 않으려고 시그니처를 유지했다.

    `window_start`·`window_end` 는 쓰지 않는다. 실시간 접안은 '지금'의 사실이라
    시간 구간이 없다. 미래 구간의 자리 상황을 알려면 예약이 있어야 하는데,
    예약을 만드는 주체가 우리가 아니다 — 그래서 우리가 아는 건 현재뿐이다.
    이 한계는 판정에 영향을 주지 않는다(모듈 docstring '이 값의 무게' 참고).
    """
    if not berth_ids:
        return {}
    rows = (
        await db.execute(
            _QUERY_LIVE_OCCUPANTS,
            {"berth_ids": berth_ids, "exclude_call_sign": exclude_call_sign},
        )
    ).mappings().all()

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["wharf_name"], []).append(
            {
                "vessel_name": row["vessel_name"] or row["callsgn"],
                # 실시간 접안에는 계획 구간이 없다. 화면이 기대하는 키는 채우되
                # 시작은 '관측 시각', 끝은 None(= 언제 뜰지 모른다)으로 둔다.
                "arrival_at_utc": row["received_at_utc"],
                "departure_at_utc": None,
                # 판정 근거를 문장에 남긴다. 거리는 '신고'로만 잡힌 배(좌표 없는
                # 부이 등)에는 없으므로 있을 때만 붙인다 — int(None) 으로 죽지 않게.
                "source_facility_name": _basis_label(row),
            }
        )
    return grouped


async def berth_capacity(db: AsyncSession, *, berth_id: str) -> int:
    """이 선석이 동시에 받을 수 있는 척수. 결측이면 1(07 문서 §4.1.1 기본값)."""
    row = (await db.execute(_QUERY_MAX_CONCURRENT_VESSELS, {"berth_id": berth_id})).first()
    # berth_vessel_count 는 double precision 이라 int 로 캐스팅해서 쓴다.
    return int(row[0]) if row and row[0] else 1
