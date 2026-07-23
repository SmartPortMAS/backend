"""
가상데이터 생성 모듈
- 탱크 저장 가능성 시뮬레이션 데이터
- 하역 작업 로그 시뮬레이션 데이터
- 인접 선석 혼재 위험 시나리오 데이터
- 선박 입출항 스케줄 데이터
- 선석 배정 현황 데이터
- 기상 이력 데이터
- MSDS 가상 DB
"""
import csv
import os
import random
import sys
from datetime import datetime, timedelta

# ─── 온산 MVP 선석 마스터 배선 (onsan_mvp 재사용) ───
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ONSAN_SCRIPTS = os.path.join(_REPO_ROOT, "onsan_mvp", "scripts")
_ONSAN_DATA = os.path.join(_REPO_ROOT, "onsan_mvp", "data")
if _ONSAN_SCRIPTS not in sys.path:
    sys.path.insert(0, _ONSAN_SCRIPTS)

from build_adjacency import mvp_berth_ids  # noqa: E402

_CARGO_TYPE_LABEL = {
    "liquid_chem": "액체화물",
    "liquid_chem_oil": "유류/액체화물",
    "petroleum": "유류",
    "crude_oil": "원유",
}


def _load_onsan_berth_list() -> list:
    """onsan_berth_master.csv -> BERTH_LIST. 후보풀은 mvp_berth_ids() 집합으로 제한한다."""
    allowed = mvp_berth_ids()
    berths = []
    with open(os.path.join(_ONSAN_DATA, "onsan_berth_master.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            berth_id = (r.get("berth_id") or "").strip()
            if berth_id not in allowed:
                continue
            length = (r.get("길이_m") or "").strip()
            dwt = (r.get("접안능력_DWT") or "").strip()
            berths.append({
                "berth_id": berth_id,
                "name": (r.get("berth_name") or "").strip(),
                "type": _CARGO_TYPE_LABEL.get((r.get("cargo_class") or "").strip(), "액체화물"),
                "max_dwt": int(dwt) if dwt else None,
                "length_m": float(length) if length else None,
                "operator": (r.get("operator") or "").strip(),
                "waterway": (r.get("수역") or "").strip(),
            })
    return berths


def _load_onsan_adjacent_pairs(berths: list) -> list:
    """onsan_adjacency_edges.csv -> (선석명, 선석명) 쌍. 기존 파일럿 수동 쌍 대체."""
    id2name = {b["berth_id"]: b["name"] for b in berths}
    pairs = []
    with open(os.path.join(_ONSAN_DATA, "onsan_adjacency_edges.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            a, b = id2name.get((r.get("a") or "").strip()), id2name.get((r.get("b") or "").strip())
            if a and b:
                pairs.append((a, b))
    return pairs


# ═══════════════════════════════════════════════════
# 온산 액체부두 선석 정보 (onsan_berth_master.csv 기반, MVP 스코프 15시설)
# ═══════════════════════════════════════════════════
BERTH_LIST = _load_onsan_berth_list()
ADJACENT_PAIRS = _load_onsan_adjacent_pairs(BERTH_LIST)

VESSEL_NAMES = [
    "ULSAN PIONEER", "PACIFIC GLORY", "DONG-A CHEMTRANS",
    "HANA TANKER", "ORIENTAL DRAGON", "GREEN OCEAN",
    "SKY MARINER", "BLUE WHALE", "STAR VOYAGER", "GOLDEN SUNRISE",
]

CARGO_LIST = [
    "벤젠", "톨루엔", "자일렌", "에탄올", "메탄올",
    "황산", "수산화나트륨", "염산", "나프타", "등유",
    "경유", "중유", "LPG", "에틸렌글리콜", "아세톤",
]


# ═══════════════════════════════════════════════════
# MSDS 가상 데이터베이스 (실제 KOSHA API 연동 전 Mock)
# ═══════════════════════════════════════════════════
CARGO_MSDS_DB = {
    "벤젠": {
        "cas_no": "71-43-2",
        "un_no": "UN1114",
        "화학명": "Benzene",
        "위험등급": "인화성 액체 (등급 2)",
        "유해성": "발암성 물질 (구분 1A), 생식세포 변이원성 (구분 1B)",
        "물리적상태": "무색 투명 액체, 특유의 방향",
        "인화점": "-11°C",
        "끓는점": "80.1°C",
        "노출기준_TWA": "0.5 ppm",
        "긴급조치": "즉시 오염 지역 격리, 점화원 제거, 방독면(유기가스용) 착용",
        "혼재금지": ["황산", "염산", "질산", "과산화수소", "강산화제"],
        "안전체크리스트": [
            "하역 전 정전기 방지 접지 확인",
            "인접 선석 화물 혼재 금지 여부 확인",
            "환기 시스템 가동 확인",
            "가스 감지기 작동 상태 점검",
            "방독면 및 보호복 비치 확인",
            "비상 샤워/세안 설비 점검",
        ],
    },
    "톨루엔": {
        "cas_no": "108-88-3",
        "un_no": "UN1294",
        "화학명": "Toluene",
        "위험등급": "인화성 액체 (등급 2)",
        "유해성": "생식독성 (구분 2), 특정표적장기독성-반복노출 (구분 2)",
        "물리적상태": "무색 투명 액체, 벤젠 유사 냄새",
        "인화점": "4°C",
        "끓는점": "110.6°C",
        "노출기준_TWA": "50 ppm",
        "긴급조치": "점화원 제거, 유기가스용 방독면 착용, 누출 시 모래로 흡착",
        "혼재금지": ["강산화제", "강산류", "질산"],
        "안전체크리스트": [
            "정전기 방지 접지 확인",
            "가스 감지기 점검",
            "환기 시스템 가동",
            "보호장구 비치 확인",
        ],
    },
    "황산": {
        "cas_no": "7664-93-9",
        "un_no": "UN1830",
        "화학명": "Sulfuric Acid",
        "위험등급": "부식성 물질 (등급 8)",
        "유해성": "피부 부식성 (구분 1A), 심한 눈 손상 (구분 1)",
        "물리적상태": "무색 유성 액체, 무취(고농도 시 자극취)",
        "인화점": "해당없음 (불연성)",
        "끓는점": "337°C",
        "노출기준_TWA": "0.2 mg/m³",
        "긴급조치": "내산성 보호복 착용, 물로 희석 금지(소량일 때), 대량 누출 시 석회 중화",
        "혼재금지": ["벤젠", "톨루엔", "메탄올", "에탄올", "나프타", "유기물 전반"],
        "안전체크리스트": [
            "내산성 배관 연결 확인",
            "인접 선석 유기용제 취급 여부 확인",
            "중화제(석회) 비치 확인",
            "보호복/보호장갑/보안경 착용",
            "누출 감지 센서 점검",
        ],
    },
    "메탄올": {
        "cas_no": "67-56-1",
        "un_no": "UN1230",
        "화학명": "Methanol",
        "위험등급": "인화성 액체 (등급 2), 급성독성 (구분 3)",
        "유해성": "경구/경피/흡입 급성독성, 시신경 손상 가능",
        "물리적상태": "무색 투명 액체, 약한 알코올 냄새",
        "인화점": "11°C",
        "끓는점": "64.7°C",
        "노출기준_TWA": "200 ppm",
        "긴급조치": "점화원 제거, 유기가스용 방독면 착용",
        "혼재금지": ["강산화제", "강산류", "과염소산"],
        "안전체크리스트": [
            "정전기 방지 접지 확인",
            "밀폐 공간 가스 농도 측정",
            "환기 시스템 가동",
            "보호장구 착용",
        ],
    },
    "에탄올": {
        "cas_no": "64-17-5",
        "un_no": "UN1170",
        "화학명": "Ethanol",
        "위험등급": "인화성 액체 (등급 2)",
        "유해성": "눈 자극성 (구분 2A)",
        "물리적상태": "무색 투명 액체, 특유 알코올 냄새",
        "인화점": "13°C",
        "끓는점": "78.4°C",
        "노출기준_TWA": "1000 ppm",
        "긴급조치": "점화원 제거, 환기 확보",
        "혼재금지": ["강산화제", "강산류"],
        "안전체크리스트": [
            "정전기 방지 접지 확인",
            "환기 확보",
            "소화기 비치 확인",
        ],
    },
    "나프타": {
        "cas_no": "8030-30-6",
        "un_no": "UN1256",
        "화학명": "Naphtha",
        "위험등급": "인화성 액체 (등급 2)",
        "유해성": "흡인유해성 (구분 1), 특정표적장기독성",
        "물리적상태": "무색~연황색 액체, 석유 냄새",
        "인화점": "-2°C",
        "끓는점": "100-200°C",
        "노출기준_TWA": "400 ppm",
        "긴급조치": "점화원 제거, 유기가스용 방독면 착용",
        "혼재금지": ["강산화제", "황산", "염산"],
        "안전체크리스트": [
            "정전기 방지 접지 확인",
            "가스 감지기 점검",
            "보호장구 착용",
            "누출 시 흡착제 사용",
        ],
    },
    "염산": {
        "cas_no": "7647-01-0",
        "un_no": "UN1789",
        "화학명": "Hydrochloric Acid",
        "위험등급": "부식성 물질 (등급 8)",
        "유해성": "피부 부식성, 심한 눈 손상, 급성독성(흡입)",
        "물리적상태": "무색~연황색 액체, 자극취",
        "인화점": "해당없음",
        "끓는점": "약 50°C (37%)",
        "노출기준_TWA": "2 ppm",
        "긴급조치": "내산성 보호복 착용, 누출 시 소다회로 중화",
        "혼재금지": ["벤젠", "톨루엔", "알코올류", "나프타", "유기물"],
        "안전체크리스트": [
            "내산성 배관 확인",
            "중화제 비치 확인",
            "보호복/보호장갑 착용",
            "가스 감지기 점검",
        ],
    },
    "자일렌": {
        "cas_no": "1330-20-7",
        "un_no": "UN1307",
        "화학명": "Xylene",
        "위험등급": "인화성 액체 (등급 3)",
        "유해성": "급성독성(흡입), 피부자극성",
        "물리적상태": "무색 투명 액체",
        "인화점": "27°C",
        "끓는점": "138-144°C",
        "노출기준_TWA": "100 ppm",
        "긴급조치": "점화원 제거, 유기가스용 방독면",
        "혼재금지": ["강산화제", "강산류"],
        "안전체크리스트": [
            "정전기 접지 확인",
            "환기 시스템 가동",
            "보호장구 착용",
        ],
    },
}


# ═══════════════════════════════════════════════════
# 혼재금지 매트릭스 (지식그래프 대체)
# ═══════════════════════════════════════════════════
PROHIBITION_MATRIX = {}
for cargo_name, msds in CARGO_MSDS_DB.items():
    PROHIBITION_MATRIX[cargo_name] = msds.get("혼재금지", [])


# ═══════════════════════════════════════════════════
# 1. 탱크 저장 가능성 시뮬레이션
# ═══════════════════════════════════════════════════
def generate_tank_simulation_data():
    """탱크 저장 가능성 시뮬레이션 데이터 생성"""
    terminals = [
        {"name": "정일 터미널", "cargos": ["벤젠", "톨루엔", "나프타"]},
        {"name": "OTK 터미널", "cargos": ["메탄올", "에탄올", "자일렌"]},
        {"name": "현대오일뱅크 터미널", "cargos": ["나프타", "경유", "등유"]},
        {"name": "SK에너지 터미널", "cargos": ["나프타", "중유", "LPG"]},
        {"name": "S-OIL 터미널", "cargos": ["나프타", "경유", "벤젠"]},
    ]
    data = []
    for terminal in terminals:
        for cargo in terminal["cargos"]:
            total_cap = random.randint(8000, 50000)
            stock_ratio = random.uniform(0.35, 0.92)
            current_stock = round(total_cap * stock_ratio, 1)
            available = round(total_cap - current_stock, 1)
            incoming = random.randint(500, 5000)
            data.append({
                "terminal_name": terminal["name"],
                "cargo_name": cargo,
                "total_capacity_ton": total_cap,
                "current_stock_ton": current_stock,
                "stock_ratio_pct": round(stock_ratio * 100, 1),
                "available_space_ton": available,
                "incoming_cargo_ton": incoming,
                "is_acceptable": available >= incoming,
            })
    return data


# ═══════════════════════════════════════════════════
# 2. 하역 작업 로그 시뮬레이션
# ═══════════════════════════════════════════════════
def generate_operation_logs(count=20):
    """하역 작업 로그 시뮬레이션 데이터 생성"""
    delay_reasons = [
        "기상악화(돌풍)", "펌프 설비 점검", "서류 확인 지연",
        "인접 선석 혼재 경고", "긴급 가스 누출 점검", "없음",
    ]
    logs = []
    for i in range(count):
        vessel = random.choice(VESSEL_NAMES)
        cargo = random.choice(CARGO_LIST)
        berth = random.choice(BERTH_LIST)
        start_time = datetime.now() - timedelta(hours=random.randint(1, 72))
        expected_hours = random.randint(8, 24)
        tonnage = random.randint(3000, 30000)
        rate = round(tonnage / expected_hours, 1)

        has_delay = random.random() < 0.3
        reason = random.choice(delay_reasons[:-1]) if has_delay else "없음"
        delay_hours = random.randint(1, 6) if has_delay else 0
        end_time = start_time + timedelta(hours=expected_hours + delay_hours)

        logs.append({
            "log_id": i + 1,
            "vessel_name": vessel,
            "cargo_name": cargo,
            "berth_name": berth["name"],
            "tonnage": tonnage,
            "unloading_rate_tph": rate,
            "start_time": start_time.isoformat(),
            "expected_end_time": (start_time + timedelta(hours=expected_hours)).isoformat(),
            "actual_end_time": end_time.isoformat(),
            "delay_hours": delay_hours,
            "delay_reason": reason,
            "warning_issued": has_delay,
        })
    return sorted(logs, key=lambda x: x["start_time"], reverse=True)


# ═══════════════════════════════════════════════════
# 3. 인접 선석 혼재 위험 시나리오
# ═══════════════════════════════════════════════════
def generate_adjacent_berth_scenarios(count=5):
    """인접 선석 작업 시나리오 생성 (onsan_adjacency_edges.csv 의 실제 ADJACENT_TO 쌍 사용)"""
    adjacent_pairs = ADJACENT_PAIRS
    scenarios = []
    for i in range(min(count, len(adjacent_pairs))):
        b1, b2 = adjacent_pairs[i]
        incoming = random.choice(CARGO_LIST)
        adjacent = random.choice(CARGO_LIST)

        # 혼재금지 체크
        prohibited = PROHIBITION_MATRIX.get(incoming, [])
        is_prohibited = adjacent in prohibited

        risk_level = "위험" if is_prohibited else "안전"

        scenarios.append({
            "scenario_id": i + 1,
            "target_berth": b1,
            "incoming_cargo": incoming,
            "incoming_vessel": random.choice(VESSEL_NAMES),
            "adjacent_berth": b2,
            "adjacent_cargo": adjacent,
            "adjacent_vessel": random.choice(VESSEL_NAMES),
            "is_prohibited": is_prohibited,
            "risk_level": risk_level,
            "reason": f"'{incoming}'와 '{adjacent}'는 혼재 금지" if is_prohibited else "혼재 위험 없음",
        })
    return scenarios


# ═══════════════════════════════════════════════════
# 4. 선박 입출항 스케줄 데이터
# ═══════════════════════════════════════════════════
def generate_vessel_schedule_data():
    """선박 입출항 스케줄 시뮬레이션"""
    statuses = ["입항 예정", "접안 중", "하역 중", "출항 완료", "대기 중"]
    vessels = []
    for i, name in enumerate(VESSEL_NAMES):
        eta = datetime.now() + timedelta(hours=random.randint(-24, 48))
        cargo = random.choice(CARGO_LIST)
        berth = random.choice(BERTH_LIST)
        status = random.choice(statuses)
        tonnage = random.randint(5000, 40000)
        vessels.append({
            "vessel_id": i + 1,
            "vessel_name": name,
            "cargo": cargo,
            "tonnage": tonnage,
            "assigned_berth": berth["name"],
            "eta": eta.isoformat(),
            "status": status,
            "dwt": random.randint(15000, 60000),
        })
    return vessels


# ═══════════════════════════════════════════════════
# 5. 선석 배정 현황
# ═══════════════════════════════════════════════════
def generate_berth_status_data():
    """선석 배정 현황 시뮬레이션"""
    berths_status = []
    for berth in BERTH_LIST:
        occupied = random.choice([True, True, False])
        berths_status.append({
            "berth_name": berth["name"],
            "berth_type": berth["type"],
            "max_dwt": berth["max_dwt"],
            "length_m": berth["length_m"],
            "is_occupied": occupied,
            "current_vessel": random.choice(VESSEL_NAMES) if occupied else None,
            "current_cargo": random.choice(CARGO_LIST) if occupied else None,
            "operation_status": random.choice(["하역 중", "접안 대기", "출항 준비"]) if occupied else "가용",
        })
    return berths_status


# ═══════════════════════════════════════════════════
# 6. 기상 이력 데이터 (최근 72시간)
# ═══════════════════════════════════════════════════
def generate_weather_history_data():
    """최근 72시간 기상 이력 시뮬레이션"""
    history = []
    now = datetime.now()
    for h in range(72):
        t = now - timedelta(hours=72 - h)
        wind = round(random.uniform(2.0, 18.0), 1)
        wave = round(random.uniform(0.2, 2.5), 1)
        vis = round(random.uniform(0.5, 15.0), 1)
        temp = round(random.uniform(5.0, 32.0), 1)

        if wind > 14.0 or wave > 1.5:
            grade = "위험"
        elif wind > 10.0 or wave > 1.0:
            grade = "주의"
        else:
            grade = "정상"

        history.append({
            "timestamp": t.isoformat(),
            "wind_speed_ms": wind,
            "wave_height_m": wave,
            "visibility_km": vis,
            "temperature_c": temp,
            "grade": grade,
        })
    return history
