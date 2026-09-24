# Smart Port Backend

This backend service provides the FastAPI foundation for the Smart Port multi-agent platform.

## Quick start (개발)

1. Copy `.env.example` to `.env`.
2. Start local infrastructure (PostgreSQL 5433 · Neo4j) from this folder:
   - `docker compose -f docker-compose.dev.yml up -d`
3. Install dependencies:
   - `pip install -r requirements.txt`
4. Run the app:
   - `uvicorn app.main:app --reload`

## 환경 (development / production)

읽는 설정 파일은 **프로세스 환경변수 `ENVIRONMENT`** 로 정한다. 파일 안의 값으로는 바꿀 수 없다.

| `ENVIRONMENT` | 읽는 파일 | DB · Neo4j |
|---|---|---|
| 미지정 / `development` | `.env` | 로컬 docker compose |
| `production` | `.env.production` (없으면 기동 실패) | RDS · Aura |

- 두 파일을 겹쳐 읽지 않는다. 운영 파일에 빠진 키가 개발 값으로 채워지지 않는다.
- 운영에서는 `CORS_ORIGINS='*'` 이면 기동 실패한다.
- 입항 판정 잡(`watch_arrivals`)이 uvicorn 프로세스 안에서 돈다. `--workers` 2 이상이면
  중복 실행되므로 운영도 workers 1. 끄려면 `ENABLE_SCHEDULER=false`.
- 운영은 Docker 없이 EC2 에서 직접 실행한다. 마이그레이션은 기동 때 자동으로 돌지 않는다 —
  배포 단계에서 명시적으로 `ENVIRONMENT=production alembic upgrade head`.

## Health check

- `GET /health`
