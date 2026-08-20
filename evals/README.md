# MSDS 챗봇 검색 평가셋

챗봇이 **어떤 근거를 끌어오는지**를 고정 문항으로 재는 도구입니다.

> ⚠️ **단위 테스트가 아닙니다. CI에 넣지 마세요.**
> 라이브 PostgreSQL·Neo4j와 `OPENAI_API_KEY`가 필요하고, 문항마다 임베딩 API를
> 호출합니다. `pytest`가 수집하지 않도록 `tests/`가 아니라 여기에 둡니다.

## 언제 돌리나

**청킹 규칙·임베딩 모델·프롬프트를 바꿨을 때.** 이 셋 중 하나라도 건드리면 검색
결과가 달라지므로, 바꾸기 전후로 돌려 회귀를 확인합니다.

```bash
cd backend
.venv/Scripts/python -m evals.run_eval            # 요약
.venv/Scripts/python -m evals.run_eval --verbose  # 실패 문항 전체
```

사전 조건: `docker start smartport-postgres-dev smartport-neo4j-dev` + 임베딩 인덱스
적재(`python -m scripts.embed_msds`).

## 무엇을 재는가

`msds_chatbot_evalset.json` — 관제사(VTS)·선박운전사 페르소나로 쓴 80문항. 화물명은
`msds_chemical` 등재분과 `aliases.py` 별칭 사전에서만 골라, 통칭(`PX`·`벙커씨유`·`IPA`·
`휘발유`)까지 함께 검증합니다.

**문항마다 정답 근거가 어느 계층에 있는지 라벨링**돼 있어 검색뿐 아니라 3계층 설계
전체를 봅니다.

| `expect_layer` | 문항 | 검증 내용 |
|---|---|---|
| `chunk` | 63 | `search_context()` top_k 안에 정답 섹션이 오는가 |
| `column` | 9 | 인화점·노출기준·용기등급·EMS가 `msds_chemical` 컬럼에 있는가 |
| `graph` | 6 | UN번호·IMDG·혼재금지·GHS가 Neo4j에 있는가 |
| `none` | 2 | 미등재 물질을 지어내지 않고 거부하는가 |

## 기준선 (2026-08-03, `text-embedding-3-large`)

```
chunk  62/63   column 9/9   graph 6/6   none 2/2   →  79/80 (98.8%)
```

**`top_k` 청크가 전부 프롬프트에 들어가므로 실질 지표는 "정답 근거 확보"입니다.**
1위 여부는 부차적입니다.

알려진 실패 1건 — `아크릴로니트릴 "피해야 할 조건이 뭐가 있어?"`. `detail02`의 P문구
(`예방` 417자 + `대응` 515자)가 만능 텍스트라 top6 6칸 중 3칸을 차지합니다. 원문 구성
문제라 모델을 바꿔도 풀리지 않습니다.

## 문항을 추가·수정할 때

**원문에 답이 실제로 있는지 먼저 확인하세요.** 초안에서 "스티렌 저장 주의사항 →
detail07"로 라벨링했는데 스티렌의 detail07 원문이 `자료없음`이라 청크 자체가
없었습니다. 답이 존재하지 않는 질문을 검색 실패로 오인하게 됩니다.

```sql
SELECT i->>'msdsItemNameKor', left(i->>'itemDetail', 60)
FROM msds_chemical, jsonb_array_elements(msds_payload->'detail07'->'data') i
WHERE name_ko = '스티렌';
```

교체·수정 사유는 JSON의 `note` 필드에 남깁니다.

## 한계

- 작성자가 만든 문항이라 **현업 검수를 받지 않았고**, 어떤 유형이 자주 들어오는지
  **가중치가 없어** 드문 질문과 흔한 질문이 1:1로 계산됩니다. 실제 질문 로그가 모이면
  그것으로 교체하는 게 맞습니다.
- **플래너(LLM 1차 호출)를 건너뜁니다.** `search_context()`를 직접 부르므로 질문 분류
  단계의 버그는 잡지 못합니다 — 실제로 환경 질문을 `out_of_scope`로 거절하던 문제가
  이 평가셋을 통과하고도 살아 있었습니다.
- **벡터 물질명 매칭 경로가 검증되지 않습니다.** 화물명을 등재명·별칭에서만 골라
  결정적 매칭만 탑니다.

배경과 측정 근거: [06_MSDS_지식배치_설계문서.md](../../06_MSDS_지식배치_설계문서.md) 7장
