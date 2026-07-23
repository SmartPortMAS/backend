from apscheduler.schedulers.background import BackgroundScheduler
import httpx
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()

# API Keys (환경변수에서 로드)
UPA_API_KEY = os.getenv("UPA_API_KEY", "YOUR_UPA_API_KEY")
PORT_MIS_API_KEY = os.getenv("PORT_MIS_API_KEY", "YOUR_PORT_MIS_API_KEY")
KMA_API_KEY = os.getenv("KMA_API_KEY", "9B_9K9QqQ4qf_SvUKrOKfw")
MSDS_API_KEY = os.getenv("MSDS_API_KEY", "53f79b3be64bc924ecc2dcb9ba2fe34f9c7b44793c1b98b99bb4786236dd9d83")

async def fetch_upa_waitst_data():
    """울산항만공사 체선정보 API 수집"""
    logger.info("Fetching UPA waitst data...")
    url = "https://www.upa.or.kr/data/portal/openapi/waitst"
    params = {"serviceKey": UPA_API_KEY, "numOfRows": 10, "pageNo": 1}
    # async with httpx.AsyncClient() as client:
    #     response = await client.get(url, params=params)

async def fetch_port_mis_data():
    """해양수산부 선박입출항정보(Port-MIS) API 수집"""
    logger.info("Fetching Port-MIS data...")
    url = "http://apis.data.go.kr/1192000/Vessels/getVesselsList"
    params = {"ServiceKey": PORT_MIS_API_KEY, "numOfRows": 10, "pageNo": 1}
    # async with httpx.AsyncClient() as client:
    #     response = await client.get(url, params=params)

async def fetch_kma_ocean_weather():
    """기상청 해양기상관측 API 수집 (API Hub)"""
    logger.info("Fetching KMA Ocean Weather data...")
    # 예: 최근 시간(tm)과 관측소(stn=0:전체) 데이터를 조회
    current_time = datetime.now().strftime("%Y%m%d%H00")
    url = "https://apihub.kma.go.kr/api/typ01/url/sea_obs.php"
    params = {
        "tm": current_time,
        "stn": "0",
        "help": "0", # 실제 데이터 수신
        "authKey": KMA_API_KEY
    }
    # async with httpx.AsyncClient() as client:
    #     response = await client.get(url, params=params)
    #     # 응답이 전문(텍스트) 형태일 경우 라인 단위 파싱 필요

async def fetch_kosha_msds_data():
    """KOSHA 물질안전보건자료(MSDS) 수집"""
    logger.info("Fetching KOSHA MSDS data...")
    url = "http://apis.data.go.kr/1180000/msds/"
    # 예: 특정 화물(벤젠 등)의 CAS 번호나 화학물질명으로 조회
    params = {
        "serviceKey": MSDS_API_KEY,
        "searchKeyword": "벤젠" # 검색어
    }
    # async with httpx.AsyncClient() as client:
    #     response = await client.get(url, params=params)

def start_scheduler():
    scheduler.add_job(fetch_upa_waitst_data, 'interval', minutes=10)
    scheduler.add_job(fetch_port_mis_data, 'interval', minutes=10)
    scheduler.add_job(fetch_kma_ocean_weather, 'interval', minutes=10)
    scheduler.add_job(fetch_kosha_msds_data, 'interval', hours=24) # MSDS는 변경이 적으므로 일 1회
    scheduler.start()
    logger.info("API Collection Scheduler started.")

def stop_scheduler():
    scheduler.shutdown()
    logger.info("Scheduler stopped.")
