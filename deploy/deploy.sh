#!/usr/bin/env bash
# 운영 EC2 에서 실행되는 배포 스크립트. GitHub Actions(deploy.yml)가 SSH stdin 으로 넘겨 실행한다.
# 서버에서 손으로 돌려도 된다:
#   SHA=<커밋> MIGRATE=0 bash deploy/deploy.sh
#
# 환경변수
#   APP_DIR   backend 체크아웃 경로            (기본 /home/ubuntu/backend)
#   SERVICE   systemd 서비스 이름              (기본 smartport-backend)
#   SHA       배포할 커밋. 비우면 origin/main 최신
#   MIGRATE   1 이면 alembic upgrade head 실행 (기본 0)
#   HEALTH_URL                                 (기본 http://127.0.0.1:8000/health)
#
# 주의
#   - git reset --hard 로 맞춘다. 서버에서 추적 파일을 손으로 고쳤다면 사라진다.
#     .env 는 .gitignore 대상이라 건드리지 않는다.
#   - /health 는 프로세스가 떠서 응답하는지만 본다(DB·Neo4j 연결은 확인하지 않는다).

main() {
  set -euo pipefail

  APP_DIR="${APP_DIR:-/home/ubuntu/backend}"
  SERVICE="${SERVICE:-smartport-backend}"
  MIGRATE="${MIGRATE:-0}"
  HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
  export PYTHONUTF8=1

  cd "$APP_DIR"
  local py="$APP_DIR/.venv/bin/python"

  [ -f .env ] || die ".env 가 없습니다: $APP_DIR/.env  (.env.prod.example 참고)"
  [ -x "$py" ] || die "venv 가 없습니다: $py  (deploy/README.md 의 서버 준비 참고)"

  local prev target
  prev="$(git rev-parse HEAD)"
  git fetch --quiet --prune origin main
  target="${SHA:-$(git rev-parse origin/main)}"

  log "현재 $(short "$prev") → 배포 $(short "$target")"
  git reset --hard --quiet "$target"

  log "의존성 설치"
  "$py" -m pip install --quiet --disable-pip-version-check -r requirements.txt

  # ── 마이그레이션 ────────────────────────────────────────────────────────
  # alembic 은 서버의 .env(운영 DB)를 읽는다.
  local migrated=0 current
  if ! current="$("$py" -m alembic current 2>&1)"; then
    printf '%s\n' "$current" >&2
    restore "$prev"
    die "alembic current 실패 — DB 에 붙지 못했을 수 있습니다. 재시작하지 않았습니다."
  fi
  if printf '%s\n' "$current" | grep -q '(head)'; then
    log "마이그레이션: 최신"
  elif [ "$MIGRATE" = "1" ]; then
    log "마이그레이션 적용 (alembic upgrade head)"
    "$py" -m alembic upgrade head
    migrated=1
  else
    log "적용되지 않은 마이그레이션이 있습니다:"
    printf '%s\n' "$current"
    "$py" -m alembic heads
    restore "$prev"
    die "MIGRATE=1 로 다시 배포하세요(Actions → Deploy (EC2) → Run workflow → migrate). 재시작하지 않았습니다."
  fi

  # ── 재시작 · 확인 ───────────────────────────────────────────────────────
  log "재시작: $SERVICE"
  sudo systemctl restart "$SERVICE"

  if wait_healthy; then
    log "배포 완료: $(short "$target")"
    return 0
  fi

  sudo journalctl -u "$SERVICE" -n 60 --no-pager >&2 || true
  if [ "$migrated" = "1" ]; then
    # 스키마가 이미 바뀌었으니 옛 코드로 되돌리면 더 깨질 수 있다. 사람이 판단한다.
    die "기동 확인 실패. 마이그레이션을 적용했으므로 자동 롤백하지 않았습니다 — 로그를 확인하세요."
  fi
  log "기동 확인 실패 — 이전 커밋 $(short "$prev") 로 되돌립니다"
  restore "$prev"
  sudo systemctl restart "$SERVICE"
  if wait_healthy; then
    die "새 버전 기동 실패, 이전 버전으로 복구했습니다."
  fi
  die "새 버전 기동 실패, 이전 버전 복구도 실패했습니다. 서버를 직접 확인하세요."
}

# 코드와 의존성을 이전 커밋으로 돌린다(서비스 재시작은 하지 않는다).
restore() {
  git reset --hard --quiet "$1"
  "$APP_DIR/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt
}

wait_healthy() {
  local i
  for i in $(seq 1 30); do
    if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

short() { printf '%s' "${1:0:7}"; }
log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] 실패: %s\n' "$*" >&2; exit 1; }

# 본문을 함수로 감싸 끝에서 부른다 — bash 가 전체를 다 읽은 뒤 실행하므로,
# 실행 중에 git reset 이 이 파일을 바꿔도 영향이 없다.
main "$@"
