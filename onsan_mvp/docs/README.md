# 온산항 액체화물 하역 관제 MVP — 설계 및 구현 가이드

멀티 에이전트 기반 액체화물 하역 스케줄링 및 안전 관제 시스템의 온산항 MVP 설계 산출물이다. 이 README는 전체 산출물의 인덱스이자 구현 로드맵이다. 상세 근거는 각 문서를 참조한다.

기준일 2026-07-20. 근거: 액체화물현황(해수부 805), 정일/오드펠 입항정보 제2판, 정박지 시설현황(803), 팀 현재상태 문서(2026-07-19), 선석배정/VTS 프로세스.

---

## 폴더 구조

```
onsan_mvp/
  docs/     설계문서 5종 + README
  data/     시드 CSV + 스크립트 생성물
  scripts/  Python 도구 (data/ 를 자동 참조)
```

스크립트는 `../data` 를 자동 탐색하므로 `onsan_mvp/scripts/` 에서 실행하든 저장소 루트에서 `python onsan_mvp/scripts/xxx.py` 로 실행하든 동작한다.

---

## 산출물 인덱스

### docs/ 설계 문서
| 파일 | 내용 |
|---|---|
| `데이터연계_및_MVP부두_재선정_검토.md` | 화물 식별, MSDS 연계 구조, 초기 부두 재선정 |
| `온산항_MVP범위_안전규칙시드.md` | 수역 스코프, 안전규칙 R1~R15, 인화성 선석배정, 그래프 스키마 |
| `현재시스템_vs_대화결정_갭분석.md` | 현재 4에이전트 시스템 대비 갭, 우선순위 |
| `선석대체_정박지대기_모델.md` | 파이프라인 전용부두 제약, 대체가능성, 정박지 대기 모델 |
| `종합정리_초기질문정답_추가도메인.md` | 초기 질문 확정 답변, 추가 도메인 고려사항 |
| `입항정보_추출_요약.md` | 12개 터미널 PDF에서 뽑은 좌표/제원/기상/게이트 요약 |

### data/ 데이터 시드 (Postgres/Neo4j 적재)
| 파일 | 적재 대상 |
|---|---|
| `onsan_berth_master.csv` | Berth 노드 (온산 액체 15시설/26선석, 제원/수역/운영사/선석수/좌표) |
| `cargo_cas_map.csv` | Chemical 노드 매핑 (화물명, CAS, UN, IMDG class, 인화점) |
| `berth_weather_thresholds.csv` | 선석별 기상 임계 (중단/이안/호스분리) |
| `onsan_anchorage.csv` | Anchorage 노드 (E1/E2/E3/W1/B, 톤수, 좌표) |
| `berth_coords_input.csv` | 좌표 입력 템플릿 |
| `onsan_adjacency_edges.csv` | 생성물: ADJACENT_TO 엣지 |
| `onsan_berth_substitutability.csv` | 생성물: SUBSTITUTABLE_WITH 엣지 |
| `onsan_berth_substitution_summary.csv` | 생성물: berth별 대체/단독선석 요약 |
| `berth_restrictions.csv` | 부두 제한(6.2): 서브선석별 전장/흘수/최대전장/최소전장/선저여유 |
| `onsan_berth_anchorage_fallback.csv` | 생성물: berth별 폴백 정박지(톤수 기준) |

### scripts/ 스크립트 (Python)
| 파일 | 역할 |
|---|---|
| `fill_berth_coords.py` | 좌표 입력(DMS/십진) 파싱 후 berth_master 병합 |
| `build_adjacency.py` | 좌표 거리 기반 ADJACENT_TO 생성 + Cypher |
| `build_substitutability.py` | 운영사/화물 기반 SUBSTITUTABLE_WITH 생성 + 단독선석 식별 |
| `weather_berth_agent.py` | 선석별 기상 판정 모듈 (기상 에이전트 배선용) |
| `check_unload_cargo.py` | getUnloadRcd 품명 컬럼/해상도 확인 (raw JSON 또는 CSV 입력) |
| `build_anchorage_assignment.py` | 톤수->정박지 배정 + berth 폴백 매핑 생성 + Cypher |

---

## 1. 확정 결정 사항

MVP 대상: 온산항 전체 액체 부두. 액체화물현황(805) 기준 12개 부두 + 부이 3기, 26선석, 접안능력 1,704,000 DWT. 벌크(온산1~4)는 액체 목록에 없어 화학 차별점이 희석되지 않는다. 본항 SK, 신항은 Phase 2.

그래프 3관계 분리:
- `HANDLES` Berth가 취급하는 화물종류
- `ADJACENT_TO` 좌표 거리 기반. 혼재/근접 위험 판정
- `SUBSTITUTABLE_WITH` 운영사/파이프라인 기반. 선석 대체 가능성

정박지 대기 모델: 액체는 전용부두라 대체가 운영사 안으로 제한된다. 배정 로직은 전용 선석 -> 같은 운영사 대체 -> 톤수 맞는 정박지 대기. 단독선석(효성, 달포, 석유공사부이)은 막히면 대기 확정.

선석별 기상: 단일 14 m/s 전역값 대신 터미널별 임계 + 중단/이안/호스분리 3단계.

탐지 원칙: 위험 탐지는 룰+그래프로 결정론, LLM은 설명 문장만.

---

## 2. 스크립트 실행 순서

```
cd onsan_mvp/scripts

# 1) 좌표 채우기: data/berth_coords_input.csv 를 채운 뒤
python fill_berth_coords.py         # -> data/onsan_berth_master.csv 갱신
# 2) 인접(혼재) 그래프
python build_adjacency.py           # -> data/onsan_adjacency_edges.csv + Cypher
# 3) 대체가능성 그래프
python build_substitutability.py    # -> substitutability(+summary).csv + 단독선석
# 기상 모듈은 import 하여 사용
python weather_berth_agent.py       # 자체 검증 실행
# getUnloadRcd 품명 확인 (팀 데이터로)
python check_unload_cargo.py <raw.json | upa_unload_record export.csv>
```

좌표는 OTK, 정일만 PDF로 확정됐다. 나머지 11개는 getGisHrbr 또는 입항정보 PDF로 확보 후 재실행하면 클러스터 내 인접이 자동 완성된다.

---

## 3. 구현 로드맵 (우선순위 + repo 모듈 매핑)

| 순위 | 작업 | repo 모듈 | 산출물 |
|---|---|---|---|
| P1 | 온산 좌표 확보 + ADJACENT_TO 재구축 + 스케줄링 후보풀 제한 | data-pipeline, Neo4j, scheduling | fill/build_adjacency, mvp_berth_ids() |
| P2 | 선석별 기상 임계 + 3단계 배선 | backend/agents/weather | weather_berth_agent.py |
| P3 | 대체가능성 + 정박지 대기 로직 (전용->대체->대기) | scheduling, orchestrator, Neo4j | build_substitutability, onsan_anchorage |
| P4 | risk_level 결정론화 (floor+격리코드+인화성) | backend/agents/safety | 갭분석 GAP5 |
| P5 | 결정론 게이트 R1~R15 (SIRE/CDI, 흘수, 인화성, 벤젠5%) | backend/agents/safety | 안전규칙시드 |
| P6 | 대기예측(점유시간)+조석창 | scheduling, data-pipeline | 종합정리 PartB |

이송률 참고: 입항정보 9.5는 "최대 이송율은 사전 이송 회의에서 협의 확정"이라 고정값이 없다. 따라서 대기예측(점유시간)은 이송률 계산이 아니라 upa_unload_record의 실제 하역 소요시간 이력으로 추정한다. 조석창(대형선 만조 입출항)은 tide_obs와 결합한다.

P1이 차별점(혼재/대체 추론)이 실제로 발화하는 최소조건이다. P2는 데이터가 이미 있어 즉시 가능. P6은 대기시간 정량화로 프로젝트 핵심 가치를 완성한다.

---

## 4. 제안 커밋/PR 분할

1. `feat(data): 온산 액체부두 마스터 + 정박지 + 화물매핑 시드` — onsan_berth_master, onsan_anchorage, cargo_cas_map, berth_weather_thresholds
2. `feat(graph): 인접/대체 그래프 생성 스크립트` — build_adjacency, build_substitutability, fill_berth_coords
3. `feat(weather): 선석별 기상 3단계 판정 모듈` — weather_berth_agent + 에이전트 배선
4. `feat(scheduling): 전용->대체->정박지 대기 로직` — 후보풀 제한 + 대체탐색 + 정박지 배정
5. `feat(safety): 결정론 게이트 R1~R15 + risk_level 결정론화`
6. `docs: 온산 MVP 설계문서 5종 + README`

---

## 5. 남은 확인 작업 (getUnloadRcd 품명)

원격 웹으로는 UPA API 명세가 세션 뒤에 있어 필드를 확정하지 못했다. 다만 팀 현재상태 문서의 6-1① 한계(화물 상세/UN은 getIntgCag가 필요하다고 명시)를 보면, getUnloadRcd의 화물 해상도는 MSDS 매칭에 부족할 가능성이 크다. 부족하지 않았다면 bzentyCd 블로커가 애초에 문제되지 않았을 것이기 때문이다.

확정은 팀 데이터로 한다. `check_unload_cargo.py` 에 raw JSON 또는 upa_unload_record export를 넣으면 품명 컬럼 유무와 해상도(구체 화물명 vs 카테고리)를 판정한다. Postgres에서는 `SELECT DISTINCT <품명컬럼> FROM upa_unload_record;` 로 바로 확인 가능하다. 이 하나로 화물 식별 소스가 확정된다.
