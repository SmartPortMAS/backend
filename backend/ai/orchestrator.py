import logging
from ai.weather_logic import check_weather_conditions
from ai.berth_assignment import resolve_berth_assignment
from ai.safety_gates import run_safety_gates, adjacent_berth_names
from simulator.data_generator import generate_berth_status_data, BERTH_LIST

logger = logging.getLogger(__name__)

# 스케줄링 후보풀: mvp_berth_ids() 로 제한된 온산 MVP 선석만 (BERTH_LIST 가 이미 필터됨)
MVP_BERTH_NAMES = {b["name"] for b in BERTH_LIST}

class AgenticOrchestrator:
    def __init__(self):
        pass

    def run_workflow(self, vessel_info: dict):
        """
        Agentic Workflow: 효율과 안전의 이중 검증 체계
        """
        logger.info(f"오케스트레이터 시작: {vessel_info['name']}")
        
        # 1. 기상분석 에이전트 호출
        weather_status = check_weather_conditions(
            vessel_info.get('wind_speed', 5.0),
            vessel_info.get('wave_height', 0.5),
            vessel_info.get('visibility', 10.0)
        )
        
        if weather_status == "불가":
            return {
                "status": "REJECTED",
                "reason": "기상 악화로 인한 접안 및 하역 불가",
                "weather_grade": weather_status
            }

        # 2. 스케줄링 에이전트 호출 (가용 선석 탐색)
        # 후보풀 제한: 온산 MVP 스코프 밖 선석(신항/본항 등)은 배정 불가
        target_berth = vessel_info.get('berth')
        if target_berth not in MVP_BERTH_NAMES:
            return {
                "status": "REJECTED",
                "reason": f"선석({target_berth})은 온산 MVP 후보풀 밖입니다. 후보: 온산 액체부두 {len(MVP_BERTH_NAMES)}개 시설",
                "weather_grade": weather_status,
            }

        # 전용 -> 같은 운영사 대체(SUBSTITUTABLE_WITH, product/dwt/draught 게이트) -> 정박지 대기
        berths = generate_berth_status_data()
        decision = resolve_berth_assignment(
            target_berth_name=target_berth,
            cargo_name=vessel_info['cargo'],
            dwt=vessel_info.get('dwt'),
            draught=vessel_info.get('draught'),
            berth_statuses=berths,
        )

        if decision["path"] == "정박지대기":
            return {
                "status": "WAITING_ANCHORAGE",
                "reason": f"전용/대체 선석 모두 불가. 정박지 '{decision['anchorage']}' 대기",
                "anchorage": decision["anchorage"],
                "berth_decision": decision,
                "weather_grade": weather_status,
            }

        assigned_berth = decision["assigned_berth"]

        # 3. 안전관제 에이전트 호출 - 결정론 게이트 R1~R15 전부 실행 (P5+P4)
        neighbor_names = adjacent_berth_names(assigned_berth)
        adjacent_operations = [
            {"berth_name": b["berth_name"], "cargo_name": b["current_cargo"], "activity": "하역중"}
            for b in berths
            if b["is_occupied"] and b["berth_name"] in neighbor_names
        ]
        assessment = run_safety_gates({
            "cargo_name": vessel_info['cargo'],
            "berth_name": assigned_berth,
            "dwt": vessel_info.get('dwt'),
            "gt": vessel_info.get('gt'),
            "draught_m": vessel_info.get('draught'),
            "loa_m": vessel_info.get('loa'),
            "sire_valid": vessel_info.get('sire_valid'),
            "cdi_valid": vessel_info.get('cdi_valid'),
            "work_hour": vessel_info.get('work_hour'),
            "benzene_pct": vessel_info.get('benzene_pct'),
            "prev_cargo_benzene_free": vessel_info.get('prev_cargo_benzene_free'),
            "weather": {"lightning": vessel_info.get('lightning', False)},
            "adjacent_operations": adjacent_operations,
        })

        if assessment["risk_level"] in ("배정불가", "위험"):
            return {
                "status": "REJECTED",
                "reason": assessment["explanation"]["summary"],
                "risk_level": assessment["risk_level"],
                "safety_assessment": assessment,
                "weather_grade": weather_status,
                "berth_decision": decision,
            }

        # 4. 최종 승인
        return {
            "status": "APPROVED",
            "vessel_name": vessel_info['name'],
            "cargo_name": vessel_info['cargo'],
            "berth_assigned": assigned_berth,
            "berth_decision": decision,
            "weather_grade": weather_status,
            "risk_level": assessment["risk_level"],
            "safety_assessment": assessment,
            "timestamp": "Now"
        }
