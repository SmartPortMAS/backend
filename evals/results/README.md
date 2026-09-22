# 임베딩 모델 A/B 재측정 (2026-08-25)

2026-08-03 최초 측정에 실행 로그가 남지 않아 재현이 불가능하다는 지적이 있어
동일 조건으로 다시 측정했다. **결과는 최초 측정과 정확히 일치한다.**

## 결과

| 모델 | 정답 섹션 1위 | top6 진입 | 전체 근거 확보 |
|---|---|---|---|
| `text-embedding-3-small` | 36/63 (57.1%) | 61/63 (96.8%) | 78/80 (97.5%) |
| **`text-embedding-3-large`** | **49/63 (77.8%)** | **62/63 (98.4%)** | **79/80 (98.8%)** |

정답 섹션 1위 차이 = 13문항 = **+20.63%p** (77.777778 − 57.142857).
문서에 적힌 `+20.7%p`는 소수 1자리로 반올림한 뒤 뺀 값이다(77.8 − 57.1).

## 조건

- backend HEAD `c69ad6a`, 워킹트리 변경 없음 (`env.txt`)
- 평가셋 `msds_chatbot_evalset.json` v1.0 (2026-08-03 작성, 커밋 `7b58ce7`)
- 대상 청크 538개, 두 arm 동일 (지문 `750e5e90309efcfdb6ef051be26ecadb`)
- `msds_chemical` 36종
- 변수는 `embedding_model` 하나뿐 — 코드 수정 없이 환경변수로만 교체

## 대조군 — 단일 변수 입증

벡터 섹션 순위를 타지 않는 17문항이 두 arm에서 완전히 동일했다.

| 계층 | 문항 | small | large |
|---|---|---|---|
| column | 9 | 9/9 | 9/9 |
| graph | 6 | 6/6 | 6/6 |
| none | 2 | 2/2 | 2/2 |

달라진 것은 chunk 계층 63문항뿐이다.

## 실패 문항

- `-large` 1건: [48] 아크릴로니트릴 "피해야 할 조건" — detail02의 P문구가 top6를
  잠식. 원문 구성 문제라 모델로 풀리지 않는다(`evals/README.md` 기존 기록과 동일).
- `-small` 2건: 위 1건 + [27] 톨루엔 "하역 중에 새면" — `-large`에서 해소.

## 재현

```bash
cd backend
EMBEDDING_MODEL=text-embedding-3-small .venv/Scripts/python -m scripts.embed_msds --rebuild
EMBEDDING_MODEL=text-embedding-3-small .venv/Scripts/python -m evals.run_eval --verbose
.venv/Scripts/python -m scripts.embed_msds --rebuild
.venv/Scripts/python -m evals.run_eval --verbose
```

embed와 eval에 **같은 환경변수**를 걸어야 한다. 어긋나면 적재 벡터와 질의 벡터의
공간이 달라져 값이 무의미해진다.

## 파일

- `env.txt` — 측정 환경(HEAD·워킹트리·DB 상태·청크 지문)
- `ab_small.log` / `ab_large.log` — 평가 실행 출력
- `embed_small.log` / `embed_large.log` — 재적재 출력
