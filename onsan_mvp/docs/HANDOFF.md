# Claude Code 핸드오프 가이드

온산 MVP 설계 산출물을 실제 코드로 옮기는 순서와 지시다. 아래 프롬프트를 순서대로 Claude Code에 붙여넣는다.

---

## 0. 준비

1. `onsan_mvp/` 폴더를 저장소 루트에 통째로 커밋한다.
2. Claude Code를 저장소 루트에서 연다(백엔드 에이전트, data-pipeline, Neo4j 코드가 보이게).
3. 아래 프롬프트를 단계별로 하나씩 준다. 한 단계 끝나고 확인 후 다음으로.

먼저 컨텍스트 프롬프트:

```
onsan_mvp/docs/README.md 와 docs/ 의 설계문서를 읽어. 온산 액체화물 하역 관제 MVP 설계야.
우리 repo 구조(data-pipeline, backend/agents, Neo4j)를 파악해줘. 아래 P1~P5를 순서대로 진행할 건데,
각 단계는 내가 따로 지시할게. 지금은 파악만.
```

---

## P2. 선석별 기상 배선 (가장 쉬움, 데이터 준비됨)

참조 파일: `scripts/weather_berth_agent.py`, `data/berth_weather_thresholds.csv`, `docs/입항정보_추출_요약.md`(2장), `docs/현재시스템_vs_대화결정_갭분석.md`(GAP6)

```
backend/agents/weather 의 단일 임계(풍속 14 m/s, 파고 1.5m) 로직을 선석별 3단계 판정으로 교체해.
scripts/weather_berth_agent.py 의 assess_berth_weather 를 그대로 쓰고 data/berth_weather_thresholds.csv 를 로드해.
/api/v1/weather/assess 가 berth_group 파라미터를 받아 해당 선석 임계로 판정하게 하고,
응답 status 를 정상/하역중단/이안/호스분리 4단계 에스컬레이션으로 확장해.
기존 forecast_warning, is_stale fail-safe 는 유지. 검증 케이스: 정일에 풍속13/파고1.2 -> 하역중단, OTK 동일입력 -> 정상.
```

---

## P1. Neo4j 온산 스코프 적재 + 스케줄링 후보풀 제한

참조 파일: `data/onsan_berth_master.csv`, `data/onsan_adjacency_edges.csv`, `scripts/build_adjacency.py`, `scripts/build_substitutability.py`, `data/onsan_berth_substitutability.csv`

```
1) data/onsan_berth_master.csv 로 Neo4j Berth 노드를 온산 스코프(15시설)로 갱신하고 좌표/수역/운영사/선석수 속성을 넣어.
2) data/onsan_adjacency_edges.csv 로 ADJACENT_TO 를 재구축해. 기존 5개 파일럿 선석군 엣지는 대체.
3) build_substitutability.py 결과(onsan_berth_substitutability.csv)로 SUBSTITUTABLE_WITH 엣지를 추가해. ADJACENT_TO 와 별도 관계로 유지.
4) 스케줄링 에이전트 후보풀을 scripts/build_adjacency.py 의 mvp_berth_ids() 집합으로 제한해서 신항/본항 선석이 추천되지 않게 해.
좌표 결측 3개(달포, SPM 부이 2기)는 그대로 두고 나머지로 진행.
```

---

## P3. 정박지 노드 + 전용→대체→대기 로직

참조 파일: `data/onsan_anchorage.csv`, `scripts/build_anchorage_assignment.py`, `data/onsan_berth_anchorage_fallback.csv`, `docs/선석대체_정박지대기_모델.md`

```
1) data/onsan_anchorage.csv 로 Anchorage 노드(E1/E2/E3/W1/B, 톤수기준, 좌표)를 적재해.
2) data/onsan_berth_anchorage_fallback.csv 로 Berth-[FALLBACK_ANCHORAGE]->Anchorage 엣지를 적재해.
3) 스케줄링/오케스트레이터 로직을 이렇게 바꿔:
   전용 선석 확인 -> 막히면 같은 운영사 SUBSTITUTABLE_WITH 대체 탐색(선박 product/dwt/draught 게이트) -> 대체도 없으면 톤수 맞는 정박지 대기.
   scripts/build_anchorage_assignment.py 의 assign_anchorage(dwt) 를 톤수->정박지 배정에 사용.
   단독선석(효성/달포/석유공사부이)은 대체 단계 없이 바로 정박지 대기.
출력에 전용/대체/대기 판단 경로와 근거를 남겨.
```

---

## P5+P4. 안전 게이트 R1~R15 + risk_level 결정론화

참조 파일: `docs/온산항_MVP범위_안전규칙시드.md`(R1~R15), `data/berth_restrictions.csv`, `data/cargo_cas_map.csv`, `docs/현재시스템_vs_대화결정_갭분석.md`(GAP5)

```
1) 안전 에이전트에 안전규칙시드의 R1~R15 를 결정론 게이트로 구현해.
   R1/R2 SIRE/CDI 검사이력 없으면 접안불가(하드블록), R3/R5 흘수/전장(data/berth_restrictions.csv),
   R6 흘수9m미만&20000GT미만 24시간 아니면 주간, R13/R14 인화성(cargo_cas_map.csv 인화점 + IMDG 격리거리 3/6/12/24m),
   R15 벤젠 5% 초과 화물 이전화물 벤젠프리 증명.
2) 최종 risk_level 을 LLM 자유판단이 아니라 (rule_engine_floor, IMDG 격리코드, 인화성등급, 게이트 히트)의
   결정론 함수로 확정해. LLM 은 checklist/reasoning/summary 문장만 생성. 같은 입력이면 같은 risk_level 이 나와야 함.
각 규칙은 Cypher 쿼리 또는 결정론 함수 1개 = 위험유형 1개. 신규 입항 시 전부 실행해 빠짐없이 탐지.
```

---

## 사람이 직접 해야 하는 것 (Claude Code가 못 함)

1. 좌표 3개 확보: 달포, S-Oil SPM 부이 2기. getGisHrbr 또는 해도. `data/berth_coords_input.csv` 채우고 `python scripts/fill_berth_coords.py` 후 `build_adjacency.py` 재실행.
2. getUnloadRcd 품명 확인: raw JSON 또는 upa_unload_record export 를 `python scripts/check_unload_cargo.py <파일>` 로 판정. 화물 식별 소스 확정.
3. 비밀값: DB 접속정보, LLM API 키를 Claude Code 환경변수로. 파일 하드코딩 금지.
4. 검증: 대한유화 안벽 481m(입항정보) vs 320m(시설현황) 실무 확인.

---

## 진행 순서 요약

사람이 1~2(좌표, getUnloadRcd)를 하는 동안 Claude Code는 P2 -> P1 -> P3 -> P5+P4 순으로 병렬 진행. 좌표가 채워지면 P1 인접을 한 번 더 재생성. 커밋은 README의 제안 PR 분할을 따른다.
