"""
멀티 에이전트 기반 액체화물 하역 스케줄링 및 안전 관제 시스템 - 백엔드 메인
기술스택: FastAPI, SQLAlchemy, APScheduler, pgvector, Neo4j, httpx
"""
from fastapi import FastAPI, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import logging

# .env 파일 로드
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from scheduler.jobs import start_scheduler, stop_scheduler
from simulator.data_generator import (
    generate_tank_simulation_data,
    generate_operation_logs,
    generate_adjacent_berth_scenarios,
    generate_vessel_schedule_data,
    generate_berth_status_data,
    generate_weather_history_data,
    CARGO_MSDS_DB,
)
from ai.orchestrator import AgenticOrchestrator
from ai.weather_logic import (
    check_weather_conditions,
    get_weather_detail,
    assess_weather,
    list_berth_groups,
)
from ai.graph_rag import evaluate_safety, get_cargo_knowledge_graph
from ai.safety_gates import run_safety_gates


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작 시 스케줄러 시작, 종료 시 스케줄러 중지"""
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="🚢 멀티 에이전트 기반 액체화물 관제 API",
    description=(
        "울산항 액체화물 하역 스케줄링, 안전관제, 기상분석 기능을 제공하는 REST API.\n"
        "- 공공데이터 API 연동 (기상청, 해양수산부, 울산항만공사, KOSHA)\n"
        "- 가상데이터 시뮬레이션 (탱크, 하역로그, 혼재위험 시나리오)\n"
        "- 멀티 에이전트 오케스트레이션 (기상/안전/스케줄링)"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = AgenticOrchestrator()


# ─────────────────── 헬스체크 ───────────────────
@app.get("/", tags=["시스템"])
def read_root():
    return {"status": "ok", "message": "울산항 액체화물 관제 API 정상 운행 중"}


# ─────────────────── 대시보드 종합 데이터 ───────────────────
@app.get("/api/dashboard", tags=["대시보드"])
def get_dashboard_data():
    """프론트엔드 대시보드 종합 조회"""
    weather = get_weather_detail()
    vessels = generate_vessel_schedule_data()
    berths = generate_berth_status_data()
    tanks = generate_tank_simulation_data()
    return {
        "weather": weather,
        "vessels": vessels,
        "berths": berths,
        "tanks": tanks,
    }


# ─────────────────── 시뮬레이션 데이터 API ───────────────────
@app.get("/api/simulation/tanks", tags=["시뮬레이션"])
def get_tank_simulation():
    """탱크 저장 가능성 시뮬레이션"""
    return generate_tank_simulation_data()


@app.get("/api/simulation/logs", tags=["시뮬레이션"])
def get_operation_log_simulation():
    """하역 작업 로그 시뮬레이션 (최근 20건)"""
    return generate_operation_logs(count=20)


@app.get("/api/simulation/scenarios", tags=["시뮬레이션"])
def get_scenario_simulation():
    """인접 선석 혼재 위험 시나리오 시뮬레이션"""
    return generate_adjacent_berth_scenarios(count=5)


@app.get("/api/simulation/vessels", tags=["시뮬레이션"])
def get_vessel_schedule():
    """선박 입출항 스케줄 시뮬레이션"""
    return generate_vessel_schedule_data()


@app.get("/api/simulation/berths", tags=["시뮬레이션"])
def get_berth_status():
    """선석 배정 현황 시뮬레이션"""
    return generate_berth_status_data()


@app.get("/api/simulation/weather-history", tags=["시뮬레이션"])
def get_weather_history():
    """최근 72시간 기상 이력 시뮬레이션"""
    return generate_weather_history_data()


# ─────────────────── AI 에이전트 API ───────────────────
@app.get("/api/v1/weather/assess", tags=["AI 에이전트"])
def assess_weather_by_berth(
    berth_group: str = Query(..., description="선석군 (예: 정일1/2부두(산암리), OTK1/2부두(처용리))"),
    wind_speed: float = Query(default=None, description="풍속(m/s)"),
    wave_height: float = Query(default=None, description="파고(m)"),
    visibility: float = Query(default=None, description="시정(km)"),
    extra_condition_active: bool = Query(default=False, description="대기정체/심한뇌우/태풍경로 등 정성조건 발효"),
    is_stale: bool = Query(default=False, description="관측값이 오래됨 (True면 판단불가 fail-safe)"),
    forecast_wind: float = Query(default=None, description="예보 풍속(m/s) - 임계 도달 시 forecast_warning"),
    forecast_wave: float = Query(default=None, description="예보 파고(m) - 임계 도달 시 forecast_warning"),
):
    """기상 에이전트 - 선석별 임계 기반 4단계 판정 (정상/하역중단/이안/호스분리)"""
    return assess_weather(
        berth_group=berth_group,
        wind_speed=wind_speed,
        wave_height=wave_height,
        visibility=visibility,
        extra_condition_active=extra_condition_active,
        is_stale=is_stale,
        forecast_wind=forecast_wind,
        forecast_wave=forecast_wave,
    )


@app.get("/api/v1/weather/berth-groups", tags=["AI 에이전트"])
def get_berth_groups():
    """선석별 기상 임계가 등록된 선석군 목록 (프론트 드롭다운용)"""
    return {"berth_groups": list_berth_groups()}


@app.get("/api/agent/weather", tags=["AI 에이전트"])
def get_weather_assessment(
    wind_speed: float = Query(default=8.2, description="풍속(m/s)"),
    wave_height: float = Query(default=1.4, description="파고(m)"),
    visibility: float = Query(default=5.0, description="시정(km)"),
):
    """(레거시) 단일 전역 임계 판정 - 선석별 판정은 /api/v1/weather/assess 사용"""
    result = check_weather_conditions(wind_speed, wave_height, visibility)
    detail = get_weather_detail(wind_speed, wave_height, visibility)
    return {"judgment": result, "detail": detail}


@app.post("/api/v1/safety/assess", tags=["AI 에이전트"])
def assess_safety_gates(arrival: dict = Body(
    ...,
    example={
        "cargo_name": "벤젠",
        "berth_name": "OTK 1부두",
        "dwt": 9000, "gt": 8000, "draught_m": 7.5, "loa_m": 120,
        "sire_valid": True, "cdi_valid": True,
        "work_hour": 14,
        "benzene_pct": 100, "prev_cargo_benzene_free": False,
        "weather": {"lightning": False, "temp_c": 22},
        "adjacent_operations": [
            {"berth_name": "OTK 2부두", "cargo_name": "나프타", "activity": "하역중"}
        ],
    },
)):
    """안전관제 에이전트 - 결정론 게이트 R1~R15 전부 실행 + risk_level 결정론 확정.
    같은 입력이면 항상 같은 risk_level. LLM 은 설명 문장만 담당(현재 결정론 템플릿)."""
    return run_safety_gates(arrival)


@app.get("/api/agent/safety", tags=["AI 에이전트"])
def get_safety_assessment(
    cargo_name: str = Query(default="벤젠", description="화물명"),
    adjacent_cargo: str = Query(default="황산", description="인접 선석 화물명"),
):
    """(레거시) 혼재위험 단건 판정 - 종합 판정은 POST /api/v1/safety/assess 사용"""
    return evaluate_safety(cargo_name, adjacent_cargo)


@app.get("/api/agent/knowledge-graph", tags=["AI 에이전트"])
def get_knowledge_graph(cargo_name: str = Query(default="벤젠")):
    """지식그래프(Neo4j 연동 준비) - 화물 관계 조회"""
    return get_cargo_knowledge_graph(cargo_name)


@app.get("/api/agent/orchestrate", tags=["AI 에이전트"])
def run_orchestration(
    vessel_name: str = Query(default="ULSAN PIONEER"),
    cargo_name: str = Query(default="벤젠"),
    berth_name: str = Query(default="OTK 1부두"),
    wind_speed: float = Query(default=8.2),
    wave_height: float = Query(default=1.4),
    dwt: float = Query(default=15000, description="재화중량톤수(DWT) - 대체 게이트/정박지 배정에 사용"),
    draught: float = Query(default=None, description="흘수(m) - 대체 선석 수심 게이트에 사용"),
    gt: float = Query(default=8000, description="총톤수(GT) - R6/R7/R8 게이트에 사용"),
    loa: float = Query(default=None, description="전장(m) - R5 게이트에 사용"),
    sire_valid: bool = Query(default=True, description="유조선 SIRE 승인 이력 (R1)"),
    cdi_valid: bool = Query(default=True, description="케미칼/LPG선 CDI 검사 이력 (R2)"),
    work_hour: int = Query(default=None, description="작업 시각(0~23시) - R6 주간제한"),
    benzene_pct: float = Query(default=None, description="벤젠 함유율(%) - R15"),
    prev_cargo_benzene_free: bool = Query(default=None, description="이전화물 벤젠프리 증명 (R15)"),
):
    """오케스트레이터 - 전체 워크플로우 실행 (기상→스케줄링[전용→대체→정박지대기]→안전)"""
    vessel_info = {
        "name": vessel_name,
        "cargo": cargo_name,
        "berth": berth_name,
        "wind_speed": wind_speed,
        "wave_height": wave_height,
        "dwt": dwt,
        "draught": draught,
        "gt": gt,
        "loa": loa,
        "sire_valid": sire_valid,
        "cdi_valid": cdi_valid,
        "work_hour": work_hour,
        "benzene_pct": benzene_pct,
        "prev_cargo_benzene_free": prev_cargo_benzene_free,
    }
    return orchestrator.run_workflow(vessel_info)


# ─────────────────── MSDS 조회 ───────────────────
@app.get("/api/msds/{cargo_name}", tags=["안전 관리"])
def get_msds_info(cargo_name: str):
    """MSDS (물질안전보건자료) 조회"""
    if cargo_name in CARGO_MSDS_DB:
        return {"found": True, "data": CARGO_MSDS_DB[cargo_name]}
    return {"found": False, "message": f"'{cargo_name}'에 대한 MSDS 정보가 없습니다."}
