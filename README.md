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

## 설정 파일 (로컬 / 운영)

설정 파일은 **기계마다 `backend/.env` 하나**다. 로컬은 dev 만, 운영 EC2 는 prod 만 돌린다.

| 기계 | `.env` 내용 |
|---|---|
| 로컬 PC | `.env.example` 기준 — 로컬 docker compose DB·Neo4j |
| 운영 EC2 | `.env.prod.example` 기준 — RDS·Aura, `ENVIRONMENT=prod` |

- 운영 값은 로컬에 `.env.prod` 로 보관한다(git 제외, 코드는 읽지 않음). 배포할 때 서버의 `.env` 에 넣는다.
- `ENVIRONMENT=prod` 이면 `CORS_ORIGINS='*'` 로는 기동 실패한다.
- 입항 판정 잡(`watch_arrivals`)이 uvicorn 프로세스 안에서 돈다. `--workers` 2 이상이면
  중복 실행되므로 운영도 workers 1. 끄려면 `ENABLE_SCHEDULER=false`.
- 운영은 Docker 없이 EC2 에서 직접 실행한다. 마이그레이션은 기동 때 자동으로 돌지 않는다 —
  배포 단계에서 명시적으로 `alembic upgrade head` (서버의 `.env` 를 읽으므로 운영 DB 에 적용된다).
- `docker-compose.dev.yml` 은 프로젝트 이름을 `smart-port-multi-agent` 로 고정한다 — 기존 로컬 데이터 볼륨 이름이다.

## 운영 배포

main 에 머지되면 GitHub Actions 가 EC2 에 자동 배포한다. 서버 준비·GitHub 설정은 [deploy/README.md](deploy/README.md).

## Health check

- `GET /health`
