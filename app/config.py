import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 환경 선택 — 개발과 운영은 DB·Neo4j·키가 완전히 분리되어 있다.
#
#   development : backend/.env             (로컬 docker compose DB·Neo4j)
#   production  : backend/.env.production  (RDS·Aura)
#
# 두 파일을 겹쳐 읽지 않고 하나만 읽는다. 겹쳐 읽으면 운영 파일에 빠진 키가
# 개발 파일 값으로 조용히 채워져, 운영 서버가 개발 DB 를 볼 수 있기 때문이다.
#
# ENVIRONMENT 는 파일이 아니라 **프로세스 환경변수**로 정한다(systemd 의
# Environment=, 셸의 export). 어느 파일을 읽을지 정하는 값을 그 파일 안에 둘 수는
# 없다. 지정하지 않으면 development.
#
# 경로는 backend 폴더 기준 절대경로다. 예전엔 env_file=".env" 상대경로라 실행
# 위치(cwd)에 따라 다른 파일을 읽거나 못 읽었다.
BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_FILES = {
    "development": BASE_DIR / ".env",
    "production": BASE_DIR / ".env.production",
}


def _selected_environment() -> str:
    env = os.getenv("ENVIRONMENT", "development").strip().lower()
    if env not in _ENV_FILES:
        raise ValueError(f"ENVIRONMENT={env!r} — development 또는 production 이어야 합니다.")
    return env


class Settings(BaseSettings):
    environment: Literal["development", "production"] = Field(default="development")
    database_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    llm_provider: Literal["gemini", "openai"] = "gemini"
    llm_model: str = "gemini-flash-latest"
    # 임베딩은 llm_provider와 독립적으로 고른다. 챗봇 RAG의 벡터 공간은 적재
    # 시점(scripts/embed_msds.py)과 조회 시점이 반드시 같은 모델이어야 하는데,
    # llm_provider는 답변 품질/비용 사정으로 자유롭게 바뀔 수 있기 때문이다.
    embedding_provider: Literal["openai"] = "openai"
    # 기본값을 바꾸면 적재된 벡터와 공간이 어긋나므로 반드시 --rebuild를 함께 돌려야
    # 한다. -large를 쓰되 dimensions=1536으로 축소해 받는다(EMBEDDING_DIM) — pgvector
    # HNSW 인덱스가 2000차원까지만 지원해서 -large 원본 3072차원은 색인할 수 없다.
    # -small 대비 실측(80문항): 정답 섹션 1위 적중 +20.7%p. 06번 설계문서 3-5절 참고.
    embedding_model: str = "text-embedding-3-large"
    kma_api_key: str | None = None
    kosha_api_key: str | None = None
    port_mis_api_key: str | None = None
    mof_api_key: str | None = None
    # 국립해양조사원 조석예보 키는 여기 없다 — 백엔드가 그 API 를 직접 부르지 않는다.
    # data-pipeline 이 받아 tide_forecast 에 적재하고, 백엔드는 그 표를 읽는다
    # (api/v1/twin.py · services/tide.py). 키는 data-pipeline/.env 의 KHOA_API_KEY.
    # 하역 개시 인터락 게이트(라즈베리파이) — MQTT 브로커(노트북 mosquitto)와 게이트→선석 대응.
    # 시연 장치는 G01 = 선석 A, G02 = 선석 B. 선석은 마스터 표기(upa_berth_facility.wharf_name).
    # 기본값은 풍속 중단 기준이 다른 두 부두(OTK1 14 m/s · 정일1 17 m/s)라 풍속 16 으로
    # A 잠김·B 열림을 보일 수 있다(하드웨어/UI연동_전달사항_20260922.md 5절).
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    gate_berths: str = '{"G01": "OTK1부두", "G02": "정일1부두"}'
    log_level: str = Field(default="INFO")
    # CORS 허용 출처. 쉼표로 구분한다(예: "https://a.vercel.app,https://b.com").
    # 개발 기본값 "*" 는 vite dev 서버 등 아무 출처나 허용한다. 운영에서는 "*" 를
    # 막는다(get_settings 에서 검사). frontend 를 같은 도메인의 nginx 프록시 뒤에 두면 교차 출처
    # 요청이 없으므로 빈 값으로 두면 된다.
    cors_origins: str = "*"
    # 입항 판정 백그라운드 잡(app/scheduler.py 의 watch_arrivals). 같은 DB 를 보는
    # 프로세스가 둘 이상이면 판정이 중복 기록되므로 끌 수 있게 둔다.
    # ※ 잡이 uvicorn 프로세스 안에서 돌기 때문에 --workers 를 2 이상으로 띄우면
    #   워커 수만큼 중복 실행된다. 운영은 workers 1 로 띄울 것.
    enable_scheduler: bool = True

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    env = _selected_environment()
    env_file = _ENV_FILES[env]
    if env == "production" and not env_file.exists():
        # 운영에서 파일이 없으면 기본값·빈 값으로 뜨는 대신 바로 죽는다.
        raise FileNotFoundError(f"운영 설정 파일이 없습니다: {env_file}")
    # 파일 안에 ENVIRONMENT=production 을 적어 두고 환경변수는 안 준 경우 등 —
    # 파일은 운영이라고 말하는데 실제로는 개발 파일을 읽은 상태라 막는다.
    # Settings 검증(CORS 등)보다 먼저 봐야 오류 메시지가 진짜 원인을 가리킨다.
    file_env = (dotenv_values(env_file).get("ENVIRONMENT") or "").strip().lower()
    if file_env and file_env != env:
        raise ValueError(
            f"ENVIRONMENT 불일치 — 프로세스 환경변수는 {env!r}({env_file.name} 을 읽음)인데 "
            f"설정 파일에는 {file_env!r} 로 적혀 있습니다. "
            "ENVIRONMENT 는 프로세스 환경변수로 지정하세요."
        )
    settings = Settings(_env_file=env_file, environment=env)
    # pydantic 검증기 안에서 던지면 오류 메시지에 입력값 전체(DB URL 포함)가 찍히므로
    # 여기서 따로 검사한다.
    if env == "production" and "*" in settings.cors_origin_list:
        raise ValueError("운영(production)에서는 CORS_ORIGINS='*' 를 쓸 수 없습니다. 허용할 도메인을 적으세요.")
    return settings
