"""판정 선석 우선순위 — 실제 접안 부두가 PORT-MIS 신고보다 먼저다 (2026-09-27).

DB 없이 _target_wharf 만 확인한다.
"""
from datetime import datetime, timedelta, timezone

from app.jobs.arrival_watcher import _target_wharf

NOW = datetime.now(timezone.utc)


def test_moored_berth_wins_over_declared():
    # 실측 사례: LAGA8 — 신고는 3부두 02, 실제로는 신항남방파제 T/S부두에 정지
    row = {"moored_wharf_name": "신항남방파제 T/S부두", "assigned_facility_name": "3부두 02"}
    assert _target_wharf(row) == ("신항남방파제 T/S부두", "AIS")


def test_declared_berth_used_before_berthing():
    row = {"moored_wharf_name": None, "assigned_facility_name": "가스부두",
           "assigned_departure_at_utc": NOW + timedelta(days=1)}
    assert _target_wharf(row) == ("가스부두", "PORT-MIS")


def test_declared_berth_ignored_after_departure_time():
    # PORT-MIS 출항 신고는 예정 시각이라, 지났을 때만 끝난 입항으로 본다
    row = {"moored_wharf_name": None, "assigned_facility_name": "가스부두",
           "assigned_departure_at_utc": NOW - timedelta(hours=1)}
    assert _target_wharf(row) == (None, "없음")


def test_anchorage_declaration_is_not_a_berth():
    row = {"moored_wharf_name": None, "assigned_facility_name": "정박지-E1"}
    assert _target_wharf(row) == (None, "없음")
