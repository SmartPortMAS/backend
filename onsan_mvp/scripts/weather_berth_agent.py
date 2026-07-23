"""
선석별 기상 판정 모듈 (GAP 6)

현재 기상 에이전트는 단일 전역 임계(풍속 14 m/s, 파고 1.5 m)로 가능/불가만 판정한다.
그러나 정일스톨트헤븐, 오드펠 입항정보 9.8의 실제 기준은 터미널마다 다르고
중단 -> 이안 -> 호스분리 3단계 에스컬레이션이다.

이 모듈은 berth_weather_thresholds.csv 를 읽어 선석별 임계를 적용하고
풍속/파고/기타조건을 종합해 심각도 최대 단계를 반환한다.
기존 /api/v1/weather/assess 의 단일 임계 로직을 대체하도록 설계했다.

입력 데이터 출처: 정일 입항정보 9.8, 오드펠 입항정보 9.8 (berth_weather_thresholds.csv)
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Optional

# 심각도 오름차순 (숫자가 클수록 심각)
LEVEL_NORMAL = "정상"
LEVEL_STOP = "하역중단"      # 하역작업 중단
LEVEL_UNBERTH = "이안"        # 부두 이안
LEVEL_DISCONNECT = "호스분리"  # 로딩암/호스 분리 (이안해도 안전한 경우)

_SEVERITY = {
    LEVEL_NORMAL: 0,
    LEVEL_STOP: 1,
    LEVEL_UNBERTH: 2,
    LEVEL_DISCONNECT: 3,
}

# 기타조건(대기정체/심한뇌우/태풍경로 등)이 걸리면 최소 이 단계 이상으로 올린다.
_EXTRA_MIN_LEVEL = LEVEL_STOP

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = next((d for d in [os.path.join(_HERE, "..", "data"), _HERE] if os.path.isdir(d)), _HERE)
_DEFAULT_CSV = os.path.join(_DATA, "berth_weather_thresholds.csv")


def _num(value: Optional[str]) -> Optional[float]:
    """빈칸/None/문자는 None 으로, 숫자는 float 로."""
    if value is None:
        return None
    value = str(value).strip()
    if value == "" or value in {"-", "N/A", "na", "NA"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass
class BerthThreshold:
    berth_group: str
    operator: str
    stop_wind: Optional[float]
    stop_wave: Optional[float]
    unberth_wind: Optional[float]
    unberth_wave: Optional[float]
    disconnect_wind: Optional[float]
    disconnect_wave: Optional[float]
    extra_conditions: str = ""
    source: str = ""


@dataclass
class WeatherVerdict:
    berth_group: str
    level: str                       # 정상 / 하역중단 / 이안 / 호스분리 / 판단불가
    reasons: list = field(default_factory=list)
    wind_ms: Optional[float] = None
    wave_m: Optional[float] = None
    thresholds_used: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "berth_group": self.berth_group,
            "level": self.level,
            "reasons": self.reasons,
            "wind": {"value": self.wind_ms, "unit": "m/s"},
            "wave": {"value": self.wave_m, "unit": "m"},
            "thresholds_used": self.thresholds_used,
        }


def load_thresholds(csv_path: str = _DEFAULT_CSV) -> dict:
    """berth_weather_thresholds.csv -> {berth_group: BerthThreshold}"""
    table: dict = {}
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            group = (row.get("berth_group") or "").strip()
            if not group:
                continue
            table[group] = BerthThreshold(
                berth_group=group,
                operator=(row.get("operator") or "").strip(),
                stop_wind=_num(row.get("중단_풍속_ms")),
                stop_wave=_num(row.get("중단_파고_m")),
                unberth_wind=_num(row.get("이안_풍속_ms")),
                unberth_wave=_num(row.get("이안_파고_m")),
                disconnect_wind=_num(row.get("호스분리_풍속_ms")),
                disconnect_wave=_num(row.get("호스분리_파고_m")),
                extra_conditions=(row.get("기타조건") or "").strip(),
                source=(row.get("source") or "").strip(),
            )
    return table


def _level_for_metric(value: Optional[float],
                      stop: Optional[float],
                      unberth: Optional[float],
                      disconnect: Optional[float]) -> tuple:
    """단일 지표(풍속 또는 파고)가 어느 단계를 넘겼는지. (level, 넘긴 임계값) 반환."""
    if value is None:
        return LEVEL_NORMAL, None
    if disconnect is not None and value >= disconnect:
        return LEVEL_DISCONNECT, disconnect
    if unberth is not None and value >= unberth:
        return LEVEL_UNBERTH, unberth
    if stop is not None and value >= stop:
        return LEVEL_STOP, stop
    return LEVEL_NORMAL, None


def _max_level(*levels: str) -> str:
    return max(levels, key=lambda lv: _SEVERITY.get(lv, 0))


def assess_berth_weather(berth_group: str,
                         wind_ms: Optional[float],
                         wave_m: Optional[float],
                         thresholds: dict,
                         extra_condition_active: bool = False) -> WeatherVerdict:
    """
    선석별 기상 판정.

    berth_group: berth_weather_thresholds.csv 의 berth_group 값
    wind_ms, wave_m: 관측 또는 예보값
    thresholds: load_thresholds() 결과
    extra_condition_active: 대기정체/심한뇌우/태풍경로 등 정성 조건 발효 여부

    반환: WeatherVerdict (level = 심각도 최대 단계)
    """
    row = thresholds.get(berth_group)
    if row is None:
        return WeatherVerdict(
            berth_group=berth_group,
            level="판단불가",
            reasons=[f"'{berth_group}' 임계값 미등록. berth_weather_thresholds.csv 확인 필요"],
            wind_ms=wind_ms,
            wave_m=wave_m,
        )

    wind_level, wind_hit = _level_for_metric(wind_ms, row.stop_wind, row.unberth_wind, row.disconnect_wind)
    wave_level, wave_hit = _level_for_metric(wave_m, row.stop_wave, row.unberth_wave, row.disconnect_wave)

    level = _max_level(wind_level, wave_level, LEVEL_NORMAL)
    reasons = []
    if wind_level != LEVEL_NORMAL:
        reasons.append(f"풍속 {wind_ms} m/s >= {wind_hit} m/s -> {wind_level}")
    if wave_level != LEVEL_NORMAL:
        reasons.append(f"파고 {wave_m} m >= {wave_hit} m -> {wave_level}")

    if extra_condition_active:
        level = _max_level(level, _EXTRA_MIN_LEVEL)
        cond = row.extra_conditions or "대기정체/심한뇌우/태풍경로"
        reasons.append(f"정성조건 발효({cond}) -> 최소 {_EXTRA_MIN_LEVEL}")

    if not reasons:
        reasons.append("모든 임계값 이내, 정상")

    return WeatherVerdict(
        berth_group=berth_group,
        level=level,
        reasons=reasons,
        wind_ms=wind_ms,
        wave_m=wave_m,
        thresholds_used={
            "stop": {"wind": row.stop_wind, "wave": row.stop_wave},
            "unberth": {"wind": row.unberth_wind, "wave": row.unberth_wave},
            "disconnect": {"wind": row.disconnect_wind, "wave": row.disconnect_wave},
            "source": row.source,
        },
    )


if __name__ == "__main__":
    import json

    th = load_thresholds()
    print("등록된 선석군:", list(th.keys()))
    print()

    # 검증 케이스: 같은 기상(풍속 15 m/s, 파고 1.2 m)을 정일과 OTK에 각각 적용
    # 현재 단일 14 m/s 임계라면 둘 다 '불가'로 동일하게 나오지만,
    # 선석별 임계에서는 정일과 OTK 판정이 갈려야 정상이다.
    cases = [
        # 동일 기상(풍속 13, 파고 1.2)인데 판정이 갈린다: 정일은 파고 1.0 기준이라 중단,
        # OTK는 풍속 14/파고 2.0 기준이라 정상. 현재 단일 14/1.5 임계면 둘 다 정상으로 오판.
        ("정일1/2부두(산암리)", 13.0, 1.2, False),
        ("OTK1/2부두(처용리)", 13.0, 1.2, False),
        ("정일1/2부두(산암리)", 18.0, 0.5, False),   # 풍속 이안 단계
        ("OTK1/2부두(처용리)", 22.0, 3.1, False),    # 풍속/파고 호스분리
        ("정일1/2부두(산암리)", 5.0, 0.4, True),      # 정상 기상이나 태풍경로 발효
        ("없는선석", 10.0, 0.5, False),
    ]
    for group, wind, wave, extra in cases:
        v = assess_berth_weather(group, wind, wave, th, extra_condition_active=extra)
        print(f"[{group}] 풍속 {wind}, 파고 {wave}, 정성조건 {extra}")
        print("  ->", v.level)
        for r in v.reasons:
            print("     -", r)
        print()
