# 운영 배포 (EC2 · GitHub Actions)

운영은 Docker 없이 EC2 에서 uvicorn 을 systemd 로 직접 띄운다. DB 는 RDS PostgreSQL, 그래프는 Neo4j Aura.

```
dev ──PR──▶ main (머지 = push)
                │
   GitHub Actions: Deploy (EC2)
     1. CI (ci.yml)   의존성 설치 · import · alembic head 1개 · 스크립트 문법
     2. SSH ──▶ EC2   deploy/deploy.sh
                        git reset --hard <커밋>
                        pip install -r requirements.txt
                        alembic current 확인 ─ 미적용 있으면 → 멈춤 (migrate 체크 시에만 적용)
                        systemctl restart smartport-backend
                        /health 60초 대기 ─ 실패하면 → 이전 커밋으로 자동 복구
```

| 파일 | 역할 |
|---|---|
| `.github/workflows/ci.yml` | PR(dev·main 대상)·dev push 때 검사. 배포 전에도 실행된다 |
| `.github/workflows/deploy.yml` | main push 또는 수동 실행 → EC2 배포 |
| `deploy/deploy.sh` | 서버에서 실행되는 배포 본체. 서버에서 손으로 돌려도 된다 |
| `deploy/smartport-backend.service` | systemd 서비스 정의 |

> CI 는 **테스트가 아니다.** `tests/` 가 비어 있어서 "새로 설치해도 앱이 import 되는가"와
> "마이그레이션 head 가 하나인가"만 본다. 기능이 맞는지는 검사하지 않는다.

---

## 1. EC2 서버 준비 (한 번만)

아래는 **Ubuntu 22.04, 기본 사용자 `ubuntu`, 경로 `/home/ubuntu/backend`** 기준이다.
다르게 쓰면 서비스 파일과 GitHub 변수(3절)를 같이 바꾼다.

### 1-1. 인스턴스 · 네트워크

- **Elastic IP 를 붙인다.** 안 붙이면 인스턴스를 껐다 켤 때 공인 IP 가 바뀌어 배포 SSH 가 깨진다.
- 보안 그룹 인바운드
  - `22` — GitHub Actions 러너가 접속한다. 러너 IP 는 고정돼 있지 않아 범위를 좁히기 어렵다.
    키 인증만 쓰고 비밀번호 로그인을 끈다(Ubuntu AMI 기본값이 이미 꺼져 있음).
  - `80` / `443` — nginx (1-6).
  - `8000` 은 **열지 않는다.** uvicorn 은 127.0.0.1 에만 붙는다.
- RDS 보안 그룹: 인바운드 `5432` 를 **EC2 의 보안 그룹**에서 허용.
- Python 버전: Ubuntu 22.04 기본이 3.10 으로 로컬·CI 와 같다. 다른 버전(예: 24.04 의 3.12)에서
  `requirements.txt` 가 설치·동작하는지는 **확인하지 않았다** — 특히 `sqlalchemy==2.0.4` 가 오래된 버전이다.

### 1-2. 패키지

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip curl nginx
```

### 1-3. 코드 받기

레포는 public 이라(2026-09-24 전환) 서버에 GitHub 인증이 필요 없다. HTTPS 로 받는다.

```bash
git clone https://github.com/SmartPortMAS/backend.git ~/backend
cd ~/backend
git checkout main      # 레포 기본 브랜치가 dev 라 clone 직후엔 dev 다
```

> 레포를 다시 private 으로 돌리면 서버의 `git fetch` 가 실패해 배포가 멈춘다. 그때는 읽기 전용
> Deploy key(조직 설정에서 허용 필요)나, Actions 가 서버로 push 하는 방식으로 바꿔야 한다.

### 1-4. venv · .env

```bash
cd ~/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`.env` 는 로컬에 보관 중인 `.env.prod`(`.env.prod.example` 기준) 내용을 서버에 넣는다. git 으로 오지 않는다.

```bash
# 로컬 PC (Git Bash) 에서
scp backend/.env.prod ubuntu@<EC2>:/home/ubuntu/backend/.env

# 서버에서 — 윈도우에서 만든 파일이면 줄끝 정리
cd ~/backend && sed -i 's/\r$//' .env && chmod 600 .env
```

`.env` 에서 운영에 꼭 맞춰야 하는 값:
- `ENVIRONMENT=prod` — 이때 `CORS_ORIGINS='*'` 면 기동이 실패한다.
- `CORS_ORIGINS` — frontend 를 같은 도메인 nginx 뒤에 두면 비워 둔다. 따로 호스팅하면 그 도메인을 적는다.
- `ENABLE_SCHEDULER=true` — 같은 DB 를 보는 다른 백엔드 프로세스가 있으면 한쪽은 `false`.

### 1-5. DB 스키마 처음 올리기

RDS 에 확장 `vector` · `btree_gist` 가 필요하다(alembic 0006 · 0010). 마이그레이션이 만들다 권한 때문에
실패하면 RDS 마스터 계정으로 먼저 `CREATE EXTENSION` 한다.

```bash
cd ~/backend
.venv/bin/python -m alembic current     # 운영 DB 에 붙는지 먼저 확인 (.env 를 읽는다)
.venv/bin/python -m alembic upgrade head
```

> backend 가 만드는 건 Alembic 소유 테이블뿐이다. `upa_*` · `mart.*` 는 data-pipeline 이 만든다
> (`alembic/env.py` 머리 주석). 운영 DB 에 그 데이터가 들어오는 경로는 이 문서 범위 밖이다.

### 1-6. systemd 서비스

```bash
sudo cp ~/backend/deploy/smartport-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smartport-backend
curl -s http://127.0.0.1:8000/health      # {"status":"ok"}
journalctl -u smartport-backend -f        # 로그
```

`--workers 1` 을 바꾸지 않는다. 입항 판정 잡이 프로세스 안에서 돌아 워커 수만큼 중복 실행된다.

**sudo 권한** — `deploy.sh` 가 `sudo systemctl restart` 와 `sudo journalctl` 을 비밀번호 없이 불러야 한다.
Ubuntu AMI 의 `ubuntu` 사용자는 이미 전부 NOPASSWD 라 따로 할 일이 없다. 배포 전용 사용자를
만든다면 그 사용자에게만 필요한 것을 준다:

```bash
sudo tee /etc/sudoers.d/smartport-deploy <<'EOF'
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart smartport-backend, /usr/bin/journalctl -u smartport-backend *
EOF
sudo chmod 440 /etc/sudoers.d/smartport-deploy && sudo visudo -c
```

### 1-7. nginx (외부 노출)

`/ws/gate` · `/ws/hardware` 가 WebSocket 이라 Upgrade 헤더를 넘겨야 한다.

```nginx
# /etc/nginx/sites-available/smartport
server {
    listen 80;
    server_name <도메인 또는 _>;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;          # 에이전트·LLM 응답이 길 수 있다
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/smartport /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
# 도메인이 있으면 HTTPS: sudo apt install -y certbot python3-certbot-nginx && sudo certbot --nginx
```

frontend 를 같은 서버에서 서빙할지, API 경로를 `/api` 아래로 둘지는 frontend 쪽 배포 방식에 달렸다 — 아직 정해지지 않았다.

---

## 2. GitHub Actions → EC2 SSH 키

배포용 키는 서버의 pull 용 키(1-3)와 **별개로** 만든다. 로컬에서:

```bash
ssh-keygen -t ed25519 -f gha_deploy -N "" -C "github-actions-deploy"
```

- `gha_deploy.pub` → 서버 `~/.ssh/authorized_keys` 에 한 줄 추가
- `gha_deploy`(개인키) → GitHub Secret `EC2_SSH_KEY` 로 넣고 **로컬 파일은 지운다**

호스트 키(`EC2_KNOWN_HOSTS`)는 서버에 한 번 접속해 본 로컬 PC 에서 꺼낸다:

```bash
ssh-keygen -F <EC2 공인 IP 또는 도메인>     # 출력의 '#' 로 시작하지 않는 줄
# 또는
ssh-keyscan -t ed25519 <EC2>               # 이 경우 fingerprint 를 서버의 것과 대조
#   서버에서: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

---

## 3. GitHub 레포 설정

`SmartPortMAS/backend` → Settings.

**Environments → New environment → `production`**
- 배포 전에 승인을 받고 싶으면 Required reviewers 에 사람을 넣는다.
- Deployment branches 를 `main` 으로 제한한다.

**Secrets and variables → Actions** (production environment 쪽에 넣어도 되고 레포 전체에 넣어도 된다)

| 종류 | 이름 | 값 |
|---|---|---|
| Secret | `EC2_HOST` | Elastic IP 또는 도메인 |
| Secret | `EC2_USER` | `ubuntu` |
| Secret | `EC2_SSH_KEY` | `gha_deploy` 개인키 전체(`-----BEGIN` ~ `END-----`) |
| Secret | `EC2_KNOWN_HOSTS` | 2절의 호스트 키 줄 |
| Variable | `EC2_APP_DIR` | 선택. 기본 `/home/ubuntu/backend` |
| Variable | `EC2_SERVICE` | 선택. 기본 `smartport-backend` |

앱의 `.env` 값(DB 비밀번호·API 키)은 GitHub 에 넣지 않는다. 서버의 `.env` 에만 있다.

**Branch protection (권장)** — `main` · `dev` 에 "Require status checks" 로 `CI / check` 를 걸면
검사가 깨진 PR 은 머지할 수 없다.

---

## 4. 운영

### 평소
dev → main PR 을 머지하면 끝. Actions 탭에서 진행을 본다.

### 마이그레이션이 있는 배포
새 `alembic/versions/*` 가 들어간 커밋이면 push 배포는 **재시작 전에 멈추고 실패로 끝난다**
(서버 코드는 이전 커밋으로 돌려 두므로 옛 버전이 계속 돈다). 이건 의도된 동작이다.

1. 마이그레이션 내용을 확인한다 — 특히 `DROP` · `DELETE` · 적재 키 변경.
2. Actions → **Deploy (EC2)** → Run workflow → 브랜치 `main`, **migrate 체크** → 실행.

마이그레이션을 적용한 배포가 기동에 실패하면 **자동 롤백하지 않는다**(스키마가 이미 바뀌어 옛 코드가 더
깨질 수 있다). `journalctl -u smartport-backend` 를 보고 사람이 판단한다.

### 수동 롤백
```bash
cd ~/backend
SHA=<되돌릴 커밋> bash deploy/deploy.sh
```
마이그레이션까지 되돌려야 하면 `alembic downgrade <리비전>` 을 먼저 — 각 리비전의 downgrade 가
데이터를 보존하는지는 파일마다 확인해야 한다.

### 서버에서 손으로 배포
```bash
cd ~/backend && bash deploy/deploy.sh                # origin/main 최신
cd ~/backend && MIGRATE=1 bash deploy/deploy.sh      # 마이그레이션 포함
```

### 자주 나는 실패
| 증상 | 원인 |
|---|---|
| `Host key verification failed` | `EC2_KNOWN_HOSTS` 불일치 — 인스턴스를 새로 만들었거나 IP 가 바뀜 |
| `Permission denied (publickey)` | `authorized_keys` 에 `gha_deploy.pub` 없음 / `EC2_USER` 틀림 |
| `git fetch` 에서 권한 오류 | 레포가 private 으로 돌아감(1-3) / 서버 origin 이 SSH 주소로 돼 있음 |
| `alembic current 실패` | RDS 보안 그룹 · `.env` 의 `DATABASE_URL` |
| 기동 확인 실패 → 자동 복구 | `journalctl` 출력이 Actions 로그에 찍힌다. 흔한 건 `.env` 누락 키, `CORS_ORIGINS='*'` |

---

## 5. 확인하지 않은 것

2026-09-24 작성 시점에 **실제 EC2 에 배포해 본 적이 없다.** 아래는 이 문서의 가정이다.

- 워크플로·`deploy.sh` 는 GitHub 러너·EC2 에서 돌려 보지 않았다. 로컬에서 확인한 것은
  `bash -n deploy/deploy.sh`(문법), 그리고 `.env`·DB 없이 더미 환경변수로
  `import app.main` · `alembic heads` 가 되는 것(= CI 단계가 DB 없이 돈다는 근거)뿐이다.
- Python 3.10 이외 버전에서의 설치·동작.
- `/health` 는 `{"status":"ok"}` 만 돌려준다 — DB·Neo4j 가 끊겨도 통과한다. 배포 성공이
  "운영 DB 와 정상 연결"을 뜻하지 않는다.
- MQTT 브로커 위치(운영)는 미정이다(`.env.prod.example`). 브로커가 없어도 앱은 뜬다.
- data-pipeline 을 운영에서 어디서 돌려 RDS 에 적재할지는 이 문서가 다루지 않는다.
