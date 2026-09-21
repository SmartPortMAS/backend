"""조위 — 체류 구간의 **최저 조위** (D2 ③).

왜 최저인가
  가용수심 = 해도 수심 + 조위 다. 조위는 하루 두 번 오르내리므로, 배가 붙어
  있는 동안 가장 얕아지는 순간이 안전을 정한다. 접안 순간만 보면 그 순간에는
  통과인데 몇 시간 뒤 바닥에 닿는 배를 놓친다.

  오경보 백테스트가 실제 사례를 잡아냈다(2026-09-21):

      SEA DRAGON → S-Oil 1부두
        해도 기준 여유 -0.50m
        접안 시각 조위 반영    **+0.26m**   ← 이 순간만 보면 통과
        체류 중 최저 조위 반영 **-0.21m** (09-12 01:40)  ← 실제로는 아니다

  "체류 중 최저"라는 조건이 판정을 뒤집는다. 이게 이 모듈이 있는 이유다.

왜 예보인가(관측이 아니라)
  판정 대상은 지금부터 출항까지의 **미래 구간**이다. 관측 조위(tide_obs)는
  과거만 있다. 미래를 현재 관측치로 판정하면 반나절 뒤 저조를 못 본다.

한계 — 숨기지 않는다
  ① `tide_forecast` 는 하루 4건의 **극값**(고조 2·저조 2)만 준다. 연속 곡선이
     아니라서 임의 시각의 조위는 여기서 보간한다(반일주조 근사, `_interpolate`).
  ② 예보는 **천문조**다. 기상에 의한 비조석 잔차(기압·바람·계절 해면)가 빠져
     있다. 2026-09-21 실측으로 울산 관측−예보 차이는 +0.8~34.8cm 였고 관측
     평균해면과 r=0.989 로 상관했다 — 즉 상수 보정이 아니라 계절·기상 성분이다.
     지금은 보정하지 않는다. 보정 없이도 방향은 맞고(예보가 실측보다 낮게 나와
     **보수적**이다), 보정하면 정밀해질 뿐이다.
  ③ 관측소는 울산(DT_0020) 한 곳이다. 부두별 조위차는 반영하지 못한다.
  ④ 예보 구간을 벗어난 시각은 **모른다** — None 을 돌려주고, 호출부는 그것을
     '판정불가'로 다뤄야 한다. 없는 값을 0 으로 채우지 않는다.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: 울산 조위관측소. tide_forecast 에 지금 이 관측소만 있다.
ULSAN_STATION_ID = "DT_0020"

#: 반일주조 주기가 약 12시간 25분이라, 앞뒤 1일이면 구간 양 끝의 이웃 극값을 반드시 포함한다.
_ONE_DAY = timedelta(days=1)


@dataclass(frozen=True)
class TideMinimum:
    """체류 구간의 최저 조위와 그 시각."""

    level_cm: float
    at_utc: datetime
    #: 구간 양 끝이 예보 범위 안에 완전히 들어왔는가. False 면 부분 구간만 본 것이다.
    fully_covered: bool


_QUERY_EXTREMES = text("""
    SELECT predicted_at_utc, tide_level_cm, extr_kind
    FROM tide_forecast
    WHERE station_id = :station_id
      AND predicted_at_utc BETWEEN :from_utc AND :to_utc
    ORDER BY predicted_at_utc
""")


def _interpolate(
    t: datetime,
    prev_at: datetime, prev_cm: float,
    next_at: datetime, next_cm: float,
) -> float:
    """이웃한 두 극값 사이의 조위. 반일주조를 코사인 반주기로 근사한다.

    극값 사이를 직선으로 이으면 극값 부근에서 실제보다 빠르게 변한다 — 조석은
    극값에서 기울기가 0 이기 때문이다. 코사인은 그 성질을 그대로 만족한다.
    """
    span = (next_at - prev_at).total_seconds()
    if span <= 0:
        return min(prev_cm, next_cm)
    phase = (t - prev_at).total_seconds() / span
    phase = max(0.0, min(1.0, phase))
    # phase 0 -> prev_cm, phase 1 -> next_cm, 양 끝에서 기울기 0
    return prev_cm + (next_cm - prev_cm) * (1 - math.cos(math.pi * phase)) / 2


async def min_tide_in_window(
    db: AsyncSession, *, window_start: datetime, window_end: datetime,
    station_id: str = ULSAN_STATION_ID,
) -> TideMinimum | None:
    """[window_start, window_end] 구간의 최저 조위. 예보가 없으면 None.

    최저값은 셋 중 하나다 — 구간 안의 저조 극값, 또는 구간의 양 끝(보간값).
    극값이 구간 안에 하나도 없으면 조위가 단조 변화하는 구간이므로 양 끝만 본다.
    """
    if window_end <= window_start:
        window_end = window_start

    # 양 끝을 보간하려면 구간 밖의 이웃 극값이 필요하다. 반일주조 주기가 약
    # 12시간 25분이므로 앞뒤 1일이면 이웃을 반드시 포함한다.
    rows = (
        await db.execute(
            _QUERY_EXTREMES,
            {
                "station_id": station_id,
                "from_utc": window_start.replace(microsecond=0) - _ONE_DAY,
                "to_utc": window_end.replace(microsecond=0) + _ONE_DAY,
            },
        )
    ).mappings().all()

    points = [(r["predicted_at_utc"], float(r["tide_level_cm"])) for r in rows]
    if len(points) < 2:
        return None

    first_at, last_at = points[0][0], points[-1][0]
    fully_covered = first_at <= window_start and last_at >= window_end

    def _at(t: datetime) -> float | None:
        """시각 t 의 조위. 예보 범위 밖이면 None."""
        if t < first_at or t > last_at:
            return None
        for i in range(len(points) - 1):
            a_at, a_cm = points[i]
            b_at, b_cm = points[i + 1]
            if a_at <= t <= b_at:
                return _interpolate(t, a_at, a_cm, b_at, b_cm)
        return points[-1][1]

    candidates: list[tuple[float, datetime]] = []

    start_cm = _at(window_start)
    if start_cm is not None:
        candidates.append((start_cm, window_start))
    end_cm = _at(window_end)
    if end_cm is not None:
        candidates.append((end_cm, window_end))

    # 구간 안의 극값은 전부 후보다. 저조만 보면 되지만, 극값 종류 라벨을 믿지
    # 않고 값으로 비교한다 — 라벨(extr_kind)이 틀려도 결과가 틀리지 않게.
    for at, cm in points:
        if window_start <= at <= window_end:
            candidates.append((cm, at))

    if not candidates:
        return None

    level_cm, at_utc = min(candidates, key=lambda c: c[0])
    return TideMinimum(level_cm=level_cm, at_utc=at_utc, fully_covered=fully_covered)
