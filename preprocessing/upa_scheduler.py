# -*- coding: utf-8 -*-
"""
울산항만공사(UPA) 수집 스케줄러 예시 (APScheduler)

데이터 성격에 따라 수집 주기를 분리한다 (프로젝트 계획서 '실시간 API 파이프라인').
  - 선박위치/운항정보 : 변동 큼  -> 짧은 주기 (예: 10분)
  - 화물/하역정보     : 월간 갱신 -> 하루 1회
  - 부두/정박지       : 거의 고정 -> 하루~월 1회

실행:
  pip install requests apscheduler
  set UPA_SERVICE_KEY=발급키   (Windows: set / macOS,Linux: export)
  python -m backend.preprocessing.upa_scheduler
"""
import os
import datetime as dt

from apscheduler.schedulers.blocking import BlockingScheduler

from .upa_collector import UpaClient, derive_nvgt_targets_from_position, _extract_items
from . import upa_preprocess as upa


RAW = "data/raw/upa"


def job_position_and_nvgt():
    """선박위치 + 운항정보 (짧은 주기)."""
    try:
        client = UpaClient()
        pos_path = client.collect_vessel_position()
        upa.run_pipeline("vessel_position", [f"{RAW}/upa_vessel_position_raw.json"])

        import json
        with open(pos_path, encoding="utf-8") as f:
            pos_items = _extract_items(json.load(f))
        year = dt.datetime.now().strftime("%Y")
        targets = derive_nvgt_targets_from_position(pos_items, year)
        if targets:
            client.collect_vessel_nvgt(targets)
            upa.run_pipeline("vessel_nvgt", [f"{RAW}/upa_vessel_nvgt_raw.json"])
        print(f"[{dt.datetime.now():%H:%M}] 선박위치/운항정보 갱신 완료")
    except Exception as e:  # noqa: BLE001
        # 폴백: 실패해도 직전 raw/staging 은 그대로 유지된다
        print(f"[ERROR] 선박위치/운항정보 수집 실패(직전 데이터 유지): {e}")


def job_unload():
    """하역정보 (하루 1회)."""
    try:
        UpaClient().collect_unload_record()
        upa.run_pipeline("unload_record", [f"{RAW}/upa_unload_record_raw.json"])
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 하역정보 수집 실패: {e}")


def job_master():
    """부두/정박지 (하루 1회면 충분, 거의 고정)."""
    try:
        c = UpaClient()
        c.collect_berth_facility()
        c.collect_anchorage()
        upa.run_pipeline("berth_facility", [f"{RAW}/upa_berth_facility_raw.json"])
        upa.run_pipeline("anchorage", [f"{RAW}/upa_anchorage_raw.json"])
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 부두/정박지 수집 실패: {e}")


def main():
    sched = BlockingScheduler(timezone="Asia/Seoul")
    # 변동 데이터: 10분마다
    sched.add_job(job_position_and_nvgt, "interval", minutes=10, id="position_nvgt")
    # 하역정보: 매일 04:10
    sched.add_job(job_unload, "cron", hour=4, minute=10, id="unload")
    # 마스터(부두/정박지): 매일 04:20
    sched.add_job(job_master, "cron", hour=4, minute=20, id="master")

    print("스케줄러 시작 (Ctrl+C 종료). 시작 시 1회 즉시 실행합니다.")
    job_master()
    job_unload()
    job_position_and_nvgt()
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("스케줄러 종료")


if __name__ == "__main__":
    main()
