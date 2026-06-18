# 울산항만공사(UPA) Open API 전처리 (담당: 함현우)

팀 공통 기준 문서(`공통 데이터 수집·전처리 기준 문서`)를 그대로 따르는 울산항만공사
API 6종 전처리 모듈이다. 최종 staging 결과물은 **공통 컬럼명(snake_case) · 공통
메타컬럼 · UTC 시간 · WGS84 좌표 · 품질 플래그 · port_call_id** 기준을 모두 준수한다.

## 1. 파일 구성

| 파일 | 역할 |
| --- | --- |
| `common.py` | 기준 문서 7장 공통 전처리 함수 (3팀 공용) |
| `upa_config.py` | API별 컬럼 매핑 / 숫자형 / 시간 / 기본키 / staging 파일명 설정 |
| `upa_preprocess.py` | raw 로더 + API별 파이프라인 + 실행 엔트리 |
| `upa_collector.py` | **Open API 수집기** (serviceKey로 호출 → raw 저장 → 전처리) |
| `upa_scheduler.py` | **APScheduler 주기 수집 예시** (데이터별 주기 분리) |

## 0. 빠른 시작 (실제 데이터 수집 → 전처리)

```bash
pip install -r backend/requirements.txt

# 발급키 등록 (.env.example 참고). Windows PowerShell:
$env:UPA_SERVICE_KEY="발급받은_Decoding_키"
# macOS/Linux:  export UPA_SERVICE_KEY="발급받은_Decoding_키"

# 수집 + 전처리 한 번에 실행
python -m backend.preprocessing.upa_collector
# -> data/raw/upa/*.json 저장, data/staging/*.csv 생성

# 주기적 자동 수집 (10분/일단위 분리)
python -m backend.preprocessing.upa_scheduler
```

> ⚠️ `serviceKey`는 공공데이터포털 마이페이지의 **'일반 인증키(Decoding)'** 값을
> 그대로 쓰세요. (Encoding 키를 쓰면 `%` 가 이중 인코딩돼 인증 오류가 납니다.)
> 화물정보(통합/내항)는 `bzentyCd`(업체코드) 등 필수 파라미터가 있어 자동 수집에서
> 제외돼 있습니다. 업체코드를 확보하면 `collect_intg_cargo(targets=[...])` 로 호출하세요.

## 2. 대상 API → staging 매핑

| # | API (Endpoint) | 의미 | staging 파일 |
| --- | --- | --- | --- |
| 1 | `getVtsBaseVslNvgtInfo` | 항만 내 선박 운항정보(입항 건) | `upa_port_call_stg.csv` |
| 2 | `getVslPstnInfo` | 항내 선박위치정보 | `upa_vessel_position_stg.csv` |
| 3-1 | `getIntgCagInfo` | 통합화물 정보 | `upa_cargo_manifest_stg.csv` |
| 3-2 | `getInprtCagDclrInfo` | 내항화물 정보 | `upa_cargo_manifest_stg.csv` (concat) |
| 4 | `getUnloadRcdInfo` | 선박 하역정보 | `upa_unload_record_stg.csv` |
| 5-1 | `getGisBaseHrbrFcltDtlInfo` | 부두(항만시설) 정보 | `upa_berth_facility_stg.csv` |
| 5-2 | `getGisBaseAnchrgDtlInfo` | 정박지 정보 | `upa_anchorage_stg.csv` |

> `source_system = "UPA"` 공통. `source_table` 은 원본 API명을 그대로 기록한다.

## 3. 사용법

```bash
# 1) raw 데이터를 data/raw/upa/ 에 원본 그대로 저장 (resultType=json)
#    예: data/raw/upa/upa_vessel_position_raw.json

# 2) 전체 파이프라인 실행
python -m backend.preprocessing.upa_preprocess
# -> data/staging/*.csv 생성
```

코드에서 개별 호출:

```python
from backend.preprocessing import upa_preprocess as upa

# 단일 API
upa.run_pipeline("vessel_position", ["data/raw/upa/upa_vessel_position_raw.json"])

# 화물(통합+내항)을 하나의 manifest 로 결합
upa.run_cargo_combined(
    intg_paths=["data/raw/upa/upa_intg_cargo_raw.json"],
    inprt_paths=["data/raw/upa/upa_inprt_cargo_raw.json"],
)
```

`run_pipeline` 의 api_key: `vessel_nvgt`, `vessel_position`, `intg_cargo`,
`inprt_cargo`, `unload_record`, `berth_facility`, `anchorage`.

로더는 data.go.kr 표준 구조(`response.body.items.item`)와 단건 dict / list 응답을
모두 처리한다. 페이지가 여러 개면 경로 리스트로 넘기면 자동 concat 된다.

## 4. 공통 기준 준수 사항

- **컬럼명**: 전부 snake_case 표준 컬럼으로 변환 (`mmsiNo→mmsi`, `lat→latitude`,
  `lot→longitude`, `vyg→voyage_no`, `cagItemNm→cargo_name_raw`, `dgUnCd→dg_un_no` 등)
- **시간**: 모든 일시 컬럼을 KST로 해석 후 **UTC**로 변환 (`parse_datetime_kst_to_utc`)
- **좌표**: float 변환 + 울산항 bbox(위 35.30~35.58 / 경 129.18~129.52) 검증,
  0.0·결측은 `MISSING_COORDINATE`, 범위 밖은 `OUT_OF_ULSAN_BBOX`
- **식별자**: `port_call_id = callsgn_ptent_yr_voyage_no`. voyage_no 결측 시
  입항일자로 임시 ID 생성 + `MISSING_VOYAGE_NO`
- **품질 플래그**: 결측/오류 행을 삭제하지 않고 `quality_flag`로만 표시 (raw 보존)
- **저장**: `utf-8-sig` CSV (한글 깨짐 방지)

## 5. 사용이 어렵거나 추가가 필요한 데이터 (팀 공유용)

### 5-1. 하역정보(`getUnloadRcdInfo`)가 다른 데이터와 잘 안 붙는다 ⚠️ (가장 큰 문제)
- 하역정보에는 **`callsgn`, `ptentYr`, `vyg`가 전혀 없다.** `vslNm`(선박명)과
  `blNoCn`(선화증권번호)만 있어 `port_call_id`를 만들 수 없다.
- 즉 하역 스케줄링의 핵심 테이블인데 운항/화물 테이블과 **선박명 문자열 매칭**으로만
  조인해야 함 → 동명이선·표기 차이로 정확도 떨어짐.
- **대안**: `bl_no`(선화증권번호)를 화물정보(`getIntgCagInfo`의 `blNo`)와 조인키로
  사용. 화물 manifest를 경유해 `bl_no → port_call_id`로 간접 연결하는 매핑
  테이블이 필요하다. (현재 manifest에 `bl_no` 보존해 둠)

### 5-2. 운항정보/화물정보는 호출부호+연도+항차가 **필수 파라미터**
- API 명세상 `callsgn`, `ptentYr/ptentVtsYr`, `vyg`(화물은 `bzentyCd`까지) 가 필수.
  즉 **특정 선박/항차를 이미 알고 있어야** 조회 가능 → "오늘 입항 전체 목록"을
  한 번에 못 받는다. 선박위치(`getVslPstnInfo`)·부두·정박지로 후보 callsgn을 먼저
  수집해 루프 호출하는 구조가 필요하다.
- 화물정보의 `bzentyCd`(업체코드)는 별도 확보 수단이 불명확 → 발급 범위 확인 필요.

### 5-3. 단위·코드 의미가 명세에 없다
- 화물 톤수 단위(`volTonUnitNm`: KL/MT 등)가 행마다 달라 **단위 통일 로직**이 추가로
  필요 (현재는 원본 값/단위명 보존만). 액체화물 부피↔중량 환산 기준표(비중) 필요.
- `nvgtStts`(운항상태), `ioprtVtsType`, `csclPrgrsSttsNm` 등 **코드 → 의미 매핑표**가
  명세에 없음. UPA에 코드집 요청 필요. (없으면 `UNIT_UNKNOWN` 플래그 활용)

### 5-4. 위험물 연계
- 화물정보의 `dgUnCd`(UN 번호)가 김동안님 **MSDS 데이터의 조인키**가 된다.
  manifest에 `dg_un_no`로 보존해 둠 → MSDS의 UN번호와 직접 조인 가능. 단, UN번호가
  결측인 액체화물이 많을 수 있어 `cargo_name_raw` 기반 화물명 매핑도 병행 필요.

### 5-5. 선박위치 정보의 한계
- `getVslPstnInfo`는 **업데이트 주기가 일간**이라 이영서님 AIS(실시간) 대비 위치
  신선도가 낮다. 실시간 추적은 AIS, UPA 위치는 보조/검증용으로 쓰는 게 적절.
- IMO/MMSI가 결측인 행이 있어 선박 식별은 `imo_no → mmsi → callsgn+vessel_name`
  순서(기준 문서 4-1)를 그대로 따른다.

### 5-6. 부두·정박지(GIS)
- 정적 마스터성 데이터라 시간 컬럼이 없음 → `collected_at_utc`만 관리.
- 부두 `depth_m`(수심)·`length_m`·`berth_capacity`는 **접안 가능 여부 판정**에 핵심
  이므로, 선박 흘수(draught)·길이와 비교하는 제약 로직을 스케줄러 단계에서 추가 권장.

## 6. 품질 플래그 동작 메모
- `quality_flag`는 단일 컬럼이라 한 행에 여러 문제가 있으면 **마지막에 적용된
  플래그**만 남는다(기준 문서 공통 코드 설계 그대로). 다중 사유 추적이 필요하면
  추후 `quality_flags`(리스트/비트마스크) 확장 논의 필요.
- 적용 순서: MISSING_KEY → 좌표 → 속도/침로 → 흘수 → 날짜순서.
