from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 설정 파일은 기계마다 backend/.env 하나다.
#   로컬 PC : .env 에 로컬 docker compose DB·Neo4j (ENVIRONMENT 생략 = dev)
#   운영 EC2: .env 에 RDS·Aura 값 + ENVIRONMENT=prod
# 로컬은 dev 만, 서버는 prod 만 돌리므로 파일 이름으로 환경을 고를 필요가 없다.
# 운영 값은 로컬에 .env.prod 로 보관해 두고(git 제외, 코드는 읽지 않음), 배포할 때
# 서버의 .env 에 넣는다 — .env.prod.example 참고.
#
# 경로는 backend 폴더 기준 절대경로다. 예전엔 env_file=".env" 상대경로라 실행
# 위치(cwd)에 따라 다른 파일을 읽거나 못 읽었다.
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    # prod 이면 운영용 검사(아래 get_settings)를 건다. 예전 이름 development/production 도
    # 받는다 — 기존 로컬 .env 에 ENVIRONMENT=development 가 적혀 있다.
    environment: Literal["dev", "prod"] = Field(default="dev")
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
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("environment", mode="before")
    @classmethod
    def _accept_long_names(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lower()
            return {"development": "dev", "production": "prod"}.get(v, v)
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # pydantic 검증기 안에서 던지면 오류 메시지에 입력값 전체(DB URL 포함)가 찍히므로
    # 여기서 따로 검사한다.
    if settings.environment == "prod" and "*" in settings.cors_origin_list:
        raise ValueError("운영(ENVIRONMENT=prod)에서는 CORS_ORIGINS='*' 를 쓸 수 없습니다. 허용할 도메인을 적으세요.")
    return settings
