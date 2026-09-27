"""필요 여유수심(UKC) 규칙 — 판정 잡과 대시보드가 같은 식을 쓰는지 (2026-09-27).

규칙은 app/agents/scheduling/ukc.py 와 대시보드 뷰(alembic 0034: mart.berth_draught_check ·
berth_audit) 두 곳에 있다. 뷰는 SQL 이라 파이썬 상수를 읽지 못하므로, 여기서 둘을 대조한다.
"""
import pytest
from sqlalchemy import text

from app.agents.scheduling.ukc import UKC_MIN_M, UKC_RATIO, required_ukc_m


def test_rule_is_max_of_floor_and_ratio():
    assert required_ukc_m(5.0) == UKC_MIN_M            # 작은 배 — 고정 오차 하한
    assert required_ukc_m(10.0) == pytest.approx(1.0)  # 경계
    assert required_ukc_m(12.0) == pytest.approx(12.0 * UKC_RATIO)  # 큰 배 — 비율


@pytest.mark.asyncio
async def test_dashboard_view_uses_the_same_rule(db):
    rows = (await db.execute(text("""
        SELECT vessel_draught_m::float AS dr, ukc_required_m::float AS req
        FROM mart.berth_draught_check
        WHERE vessel_draught_m IS NOT NULL
    """))).mappings().all()
    if not rows:
        pytest.skip("로컬 DB 에 흘수가 있는 접안선이 없음")
    for r in rows:
        assert r["req"] == pytest.approx(round(required_ukc_m(r["dr"]), 2)), r
