-- ===========================================================================
-- 0029: mart 스키마(뷰 20 · 구체화 뷰 1 · 함수 4) — data-pipeline/mart_views.sql 에서 이관
-- ===========================================================================
-- 2026-09-24 data-pipeline origin/dev(deb08f8)의 mart_views.sql 을 그대로 옮긴 **고정본**이다.
-- 스키마 소유권을 backend(Alembic)로 모았으므로 앞으로 mart 뷰를 바꿀 때는 이 파일을
-- 고치지 말고 새 리비전에서 CREATE OR REPLACE VIEW 로 바꾼다(이미 적용된 DB 는 이 파일을
-- 다시 실행하지 않는다).
--
-- 아래 본문의 "실행: DBeaver/psql -f" 안내와 "CREATE TABLE IF NOT EXISTS upa_*" 껍데기는
-- 이관 전 사정이다. 껍데기 두 개는 0028 이 표를 먼저 만들므로 여기서는 아무 일도 하지 않는다.
-- ===========================================================================

-- ===========================================================================
-- mart_views.sql v2 — 통합 마트 뷰 6종 (키 JOIN 기반 '한 줄 조회')
-- ===========================================================================
-- 13종 staging 테이블이 한 PostgreSQL 에 모인 뒤, 에이전트·대시보드·FastAPI 가
-- 바로 쓸 수 있도록 키 JOIN 으로 묶은 조회 전용 뷰 6개를 만든다.
--
-- v2 (설계 초안 인수·통합): 김동안 설계 초안(뷰 6개 구조)과 기존 v1(뷰 4개)을
-- 통합 — v1 의 dashboard_current 안에 숨어 있던 최신 위치/화물-MSDS 로직을
-- vessel_latest_position / cargo_msds 뷰로 분리해 중복 CTE 를 제거했고,
-- create_mart.py 에만 있던 거리·ETA 산출을 동일 공식(하버사인, R=3440.065)으로
-- dashboard_current 에 통합했다. 실제 적재 테이블 컬럼명·타입 대조 완료.
--
-- 실행: DBeaver(5433) 에서 이 파일 전체 실행 (또는 psql -f mart_views.sql)
-- 검증: mart_views_check.sql 의 검증 쿼리 5종이 모두 PASS 여야 한다.
--
-- 물리 마트(ulsan_vessel_mart, create_mart.py)와의 역할 구분:
--   뷰(여기)        = "지금" 상태 조회 — 대시보드·에이전트 실시간 서빙용
--   물리 마트 테이블 = 스냅샷 이력 축적 — 시계열 분석·모델 학습용
--   (동일 조인 로직의 이중화가 아니라 서빙/학습 용도 분리다)
--
-- ===========================================================================
-- 키 체계 (2026-08 MMSI-First 전환 이후) — ★ 여기가 자주 혼동된다
-- ===========================================================================
-- 키는 "고유키"와 "조인키"가 서로 다른 역할을 한다. 하나로 착각하면 안 된다.
--
--   [고유키 / 식별키]  vessel_uid  — "이 배가 그 배인가"
--       COALESCE(mmsi::text, 'CS:'||upper(btrim(callsgn)))
--       결측 0%인 MMSI 가 1순위. 우리 시스템이 선박을 세는 단위.
--       AIS 동적신호(Msg 1/2/3/18)에 실려오므로 위치가 있으면 항상 있다.
--       → mart.vessel_identity, mart.vessel_latest_position 의 파티션 키
--
--   [조인키 / 연계키]  callsgn     — "이 배의 서류가 어느 것인가"
--       PORT-MIS 입출항신고 · UPA 화물 manifest · 하역기록은 모두 호출부호로
--       발급된다. MMSI 로는 이 서류들을 찾을 수 없다 (원천에 MMSI 컬럼 자체가
--       없다). 따라서 서류 계열 조인은 앞으로도 callsgn 이다.
--       AIS 정적신호(Msg 5/24)에 실려오므로 미송출 선박은 결측(약 31%).
--
--   [표시키]           vessel_key  — 화면·로그용 대표 식별자
--       imo → mmsi → callsgn 우선순위로 고른 사람이 읽기 좋은 값.
--       조인에 쓰지 말 것 (우선순위가 바뀌면 값이 바뀐다).
--
--   그 외 도메인 조인키:
--       dg_un_no         — 화물 ↔ MSDS 위험물
--       bl_no            — 하역 ↔ 화물
--       facility_name    — 입항 ↔ 선석 제원(수심)
--       observed_at_utc  — 기상·조위 최신값
--
--   ★ 요약: 배를 "세는" 것은 vessel_uid, 배의 "서류를 찾는" 것은 callsgn.
--     그래서 dashboard_current 는 식별 조인만 vessel_uid 로 걸고
--     (관공선도 화면에 남기려고), 입항·화물 조인은 callsgn 을 유지한다.
--
-- 뷰 계층 (의존 순서대로 생성) — 각 단계의 조인키를 함께 표기:
--   1. mart.vessel_identity        선박 식별 마스터 (1척 = 1행)
--        위치 --vessel_uid--> 자기 자신 집계,  PORT-MIS --callsgn--> 선종
--   2. mart.vessel_latest_position 선박별 최신 위치 1행 (UPA 우선, AIS 보강)
--        UPA∪AIS --vessel_uid--> 최신 1행,  upa_port_call --callsgn--> 출항확정
--   2-1. mart.vessel_presence      지금 선석·정박지에 실제로 있는 배 (UPA 위치 판정)
--        위치 --좌표--> 선석·정박지 구역,  upa_port_call --callsgn--> 신고 선석 이름표
--   3. mart.port_call_overview     입항 통합 (입항건당 대표 1행)
--        PORT-MIS ∙ UPA운항 ∙ 화물 전부 --callsgn-->
--   4. mart.cargo_msds             화물 ↔ MSDS (★안전관제 핵심)
--        화물 --dg_un_no--> MSDS
--   5. mart.weather_now            환경 최신 1행 (기상+조위+파고+조류 스냅샷)
--        --observed_at_utc--> 각 관측 최신값
--   5-1. mart.berth_draught_check  조위 반영 가용수심 · UKC 판정
--        선석 재선(2-1) --berth_name--> 선석 수심,  --callsgn--> 흘수
--   6. mart.dashboard_current      '한 줄 조회' — 대시보드·에이전트 진입점
--        위치 --vessel_uid--> 식별 / 위치 --callsgn--> 입항·화물 / 기상 CROSS
--   7. mart.pipeline_health        수집기 생존 신호 (조인 없음, 단일 집계행)
--   8. mart.berth_current_cargo    선석별 현재 취급 화물 → chem_id (백엔드 소비 계약)
--        선석 재선(2-1) --callsgn--> 화물 --dg_un_no--> MSDS --> chem_id
--        ★ backend/app/agents/scheduling/category_map.py 의 카테고리 대표값
--          근사를 대체한다 (그 파일 주석이 이 뷰를 기다리고 있다)
--   (+ mart.msds_flat             msds_chemical JSONB 평탄화 — cargo_msds 가 사용)
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS mart;

-- ---------------------------------------------------------------------------
-- 0-A. 기존 뷰 제거 (재실행 안전성)
--
-- ★ 왜 DROP 이 필요한가 — CREATE OR REPLACE VIEW 의 제약
--   PostgreSQL 의 CREATE OR REPLACE VIEW 는 "기존 컬럼 목록 뒤에 새 컬럼을
--   덧붙이는" 변경만 허용한다. 컬럼을 중간에 끼워 넣거나, 순서·이름·타입을
--   바꾸면 다음과 같이 실패한다:
--       ERROR: cannot change name of view column "nav_status_code" to "heading"
--
--   실제로 그 사고가 났다 (2026-08-10, PR #23 리뷰에서 재현):
--   vessel_latest_position 에 heading/draught 를 sog·cog 옆(중간)에 추가했더니
--   신규 DB 에서는 통과하고 기존 DB 에서만 실패했다. 만든 사람은 신규 DB 라
--   못 보고, 받는 사람은 기존 DB 라 바로 깨지는 — 가장 늦게 발견되는 형태다.
--
--   해법으로 "새 컬럼은 항상 맨 뒤에 붙이기" 규칙을 지킬 수도 있지만,
--   그러면 cog 옆에 있어야 할 heading 이 파일 맨 끝에 홀로 떨어져 가독성이
--   나빠지고, 규칙을 한 번만 어겨도 같은 사고가 반복된다.
--   뷰는 데이터를 갖지 않으므로(정의만 있음) 지웠다 다시 만드는 비용이 0 이다.
--   구조적으로 막는 쪽을 택한다.
--
-- ★ CASCADE 를 쓰지 않는 이유
--   CASCADE 는 이 뷰에 의존하는 "우리가 모르는 객체"(백엔드가 만든 뷰 등)까지
--   조용히 같이 지운다. 여기서는 의존 역순으로 명시 삭제만 하고, 외부 의존이
--   있으면 에러로 드러나게 둔다 — 조용한 파괴보다 시끄러운 실패가 낫다.
--
-- ★ 삭제 순서 = 생성 역순 (의존하는 쪽을 먼저 지운다)
--
--   2026-09-17 수정 — facility_alias 에 기대는 berth_dwell_stats·berth_draught_check
--   가 facility_alias 보다 뒤에 지워지고 있었다. 기존 DB 에서 DROP MATERIALIZED
--   VIEW 가 "other objects depend on it" 으로 실패하고, psql 기본값(ON_ERROR_STOP
--   off)이 그 에러를 넘겨 facility_alias 가 옛 정의로 계속 남아 있었다.
--   의존하는 뷰를 모두 앞으로 옮겼다 — psql -v ON_ERROR_STOP=1 로 끝까지 통과한다.
-- ---------------------------------------------------------------------------
--   2026-09-22 추가 — 감사·승인 뷰(7절)를 여기로 들여왔고, 그 김에 순서를
--   "아무도 참조하지 않는 최상위 소비 뷰부터"로 다시 세웠다. dashboard_current 가
--   arrival_schedule 을 참조하는데 arrival_schedule 이 먼저 지워지고 있어서
--   막혔다(실측 2026-09-22). 최상위부터 지우면 이런 역전이 생기지 않는다.
--
-- 순서는 pg_depend 로 실제 의존 그래프를 위상정렬해 뽑았다(2026-09-22). 손으로
-- 짜맞추면 이번처럼 한 번에 하나씩만 드러나 세 번 막힌다. 다시 틀어지면:
--   WITH edges AS (SELECT DISTINCT dep.relname child, src.relname parent
--     FROM pg_depend d JOIN pg_rewrite r ON r.oid=d.objid
--     JOIN pg_class dep ON dep.oid=r.ev_class JOIN pg_class src ON src.oid=d.refobjid
--     JOIN pg_namespace ns ON ns.oid=src.relnamespace
--     WHERE ns.nspname='mart' AND dep.relname<>src.relname) ...
--
-- 3층 — 가장 위 (아무도 참조하지 않는다)
DROP VIEW IF EXISTS mart.berth_current_cargo;
DROP VIEW IF EXISTS mart.dashboard_current;
DROP VIEW IF EXISTS mart.pipeline_health;
DROP VIEW IF EXISTS mart.approval_candidates;
DROP VIEW IF EXISTS mart.berth_audit;
DROP VIEW IF EXISTS mart.anchorage_audit;
DROP VIEW IF EXISTS mart.berth_facility_traffic;
DROP VIEW IF EXISTS mart.berth_dwell_stats;
-- berth_occupancy_live 는 mart.vessel_presence 로 대체돼 더는 만들지 않는다(7절 주석).
DROP VIEW IF EXISTS mart.berth_occupancy_live;
-- 2층
DROP VIEW IF EXISTS mart.berth_draught_check;
DROP VIEW IF EXISTS mart.berth_occupancy;
DROP VIEW IF EXISTS mart.anchorage_limit;
DROP VIEW IF EXISTS mart.arrival_schedule;
DROP VIEW IF EXISTS mart.cargo_msds;
-- 1층
DROP VIEW IF EXISTS mart.msds_flat;
DROP VIEW IF EXISTS mart.vessel_presence;
DROP VIEW IF EXISTS mart.port_call_overview;
DROP VIEW IF EXISTS mart.vessel_latest_position;
-- 0층 — 뿌리
DROP MATERIALIZED VIEW IF EXISTS mart.facility_alias;
DROP FUNCTION IF EXISTS mart.norm_berth(text);
DROP FUNCTION IF EXISTS mart.norm_facility(text);
DROP VIEW IF EXISTS mart.weather_now;
DROP VIEW IF EXISTS mart.berth_handling_cargo;
DROP VIEW IF EXISTS mart.vessel_identity;

-- ---------------------------------------------------------------------------
-- 0. 선행 조건: 참조 테이블 존재 보장 (빈 테이블이라도)
--
-- PostgreSQL 은 CREATE VIEW 시점에 참조 테이블의 "존재"를 검사한다(데이터는 안
-- 읽는다). 따라서 테이블이 아예 없으면 뷰 등록 자체가 실패한다.
--
-- upa_cargo_manifest 는 UPA 통합화물 API(getIntgCagInfo)가 업체코드(bzentyCd)
-- 필수라 아직 자동수집이 불가해 테이블이 없는 상태다. 그 결과 화물과 직접
-- 관련이 없는 port_call_overview 까지 (5개 부가 컬럼을 LEFT JOIN 한다는 이유로)
-- 함께 생성 실패하고, 이를 참조하는 dashboard_current 까지 연쇄로 죽었다.
--
-- 아래 CREATE TABLE IF NOT EXISTS 로 "빈 껍데기"를 보장하면:
--   - 뷰 6종이 화물 데이터 없이도 정상 등록된다 (화물 컬럼만 NULL)
--   - 나중에 실데이터(또는 합성 샘플)가 적재되면 뷰 수정 없이 그대로 채워진다
--   - 이미 테이블이 있으면 이 구문은 아무 일도 하지 않는다 (기존 데이터 안전)
--
-- 컬럼 구성은 upa_config.py 의 INTG_CAG_INFO / INPRT_CAG_DCLR_INFO column_map
-- 합집합 + 공통 메타데이터를 따른다 (적재 시 스키마 불일치 방지).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS upa_cargo_manifest (
    -- ★ record_uid 는 upa_loader.TABLE_MAP 이 이 테이블의 UPSERT 자연키로 쓴다.
    --   (bl_no 등 자연키 후보가 결측 가능해 전체 행 해시를 키로 쓰는 예외 테이블)
    --   여기 껍데기에 이 컬럼이 빠져 있으면, 뷰가 먼저 테이블을 만든 환경에서
    --   로더가 "column record_uid does not exist" 로 죽는다 (실증 확인).
    --   스키마 소유권이 Alembic / 파이프라인 auto_create / 이 파일 3곳으로
    --   갈라져 있어 생기는 문제다. 일원화 전까지는 여기서 맞춰 둔다.
    record_uid                   text,
    port_code                    text,
    ptent_yr                     text,
    voyage_no                    text,
    callsgn                      text,
    vessel_name                  text,
    vessel_type_name             text,
    vessel_nationality_code      text,
    vessel_nationality_name      text,
    mrn_no                       text,
    bl_no                        text,
    master_bl_no                 text,
    io_se_code                   text,
    io_se_name                   text,
    facility_name                text,
    cargo_se_name                text,
    cargo_name_raw               text,          -- MSDS 매핑 입력
    dg_un_no                     text,          -- 위험물 UN 번호 (MSDS 조인키)
    cargo_basis                  text,          -- 합성 샘플의 화물 배정 근거 표기
    package_type_name            text,
    unload_method_name           text,
    vol_ton_unit_name            text,
    vol_ton                      double precision,
    weight_ton                   double precision,
    vol_size                     double precision,
    weight_size                  double precision,
    bulk_vol_size                double precision,
    bulk_weight_size             double precision,
    container_count              double precision,
    pod_name                     text,          -- 양하항
    pol_name                     text,          -- 적하항
    ldud_port_name               text,          -- 양적하 항구명 (내항화물)
    last_dest_port_name          text,
    arrival_at_utc               timestamptz,
    customs_progress_status_name text,
    source_system                text,
    source_table                 text,
    collected_at_utc             timestamptz,
    quality_flag                 text,
    is_synthetic                 boolean
);

-- ---------------------------------------------------------------------------
-- 0-A2. upa_berth_facility — 선석 제원 마스터 (facility_alias 가 정본으로 참조)
--
-- ★ 왜 여기 껍데기가 필요한가 (2026-08-12 실측 확인)
--   이 테이블은 UPA 부두현황 API(getGisBaseHrbrFcltDtlInfo)로 채워지는데,
--   그 수집은 run_pipeline.DOMAINS 8종(tide/wave/weather/weather_forecast/
--   vessel/port_call/portmis/mart)에 **들어 있지 않다** — collect_berth_facility()
--   를 따로 호출해야 생긴다.
--
--   그래서 표준 순서(alembic upgrade head → run_pipeline all → psql -f
--   mart_views.sql)를 그대로 따르면 이 테이블이 없고, 아래 mart.facility_alias 의
--   CREATE MATERIALIZED VIEW 가 참조 테이블 부재로 실패한다. PostgreSQL 은 뷰
--   생성 시점에 참조 테이블 "존재"를 검사하므로, 파일 앞쪽에서 죽으면
--   **뒤따르는 뷰 10종이 하나도 안 만들어진다**. 실측 재현:
--       ERROR: relation "upa_berth_facility" does not exist   → mart 뷰 0개
--
--   바로 위 upa_cargo_manifest 껍데기와 정확히 같은 이유·같은 처방이다.
--   실데이터가 이미 적재돼 있으면 이 구문은 아무 일도 하지 않는다.
--
--   ※ 껍데기만 있는 상태에서는 facility_alias 의 master CTE 가 0행이 되어 자동
--     매칭이 전부 UNMAPPED 로 떨어진다. 그건 "부두 제원을 아직 안 받았다"는
--     사실의 정확한 반영이지 조용한 오작동이 아니다 — 검증 13 이 그 상태를
--     드러낸다. 선석 매칭을 실제로 쓰려면 collect_berth_facility() 를 한 번
--     돌려야 한다.
--
-- 컬럼 구성은 upa_config.HRBR_FCLT_INFO.column_map 과 1:1 (적재 시 스키마 불일치 방지).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS upa_berth_facility (
    port_name            text,
    wharf_name           text,          -- ★ facility_alias 매칭의 정본 컬럼
    length_m             double precision,
    depth_m              double precision,
    berth_capacity       double precision,
    berth_vessel_count   double precision,
    unload_capacity      double precision,
    handling_cargo_name  text,
    wharf_se_name        text,
    latitude             double precision,
    longitude            double precision,
    port_operator_name   text,
    source_system        text,
    source_table         text,
    collected_at_utc     timestamptz,
    quality_flag         text
);

-- ---------------------------------------------------------------------------
-- 0-B. mart.facility_alias — 시설명 정규화 매핑 (P1 처방)
--
-- ★ 문제 — 실측(2026-08-11, upa_port_call 재수집 후)
--   VTS 운항관제(upa_port_call.facility_name) 113종 vs 선석 제원 마스터
--   (Neo4j Berth.wharf_name) 68종이 서로 다른 표기 체계다.
--   'S-OIL1부두'(VTS) vs 'S-Oil 1부두'(마스터), 'OTK부두' vs 'OTK1부두' 등.
--   문자열 완전일치로 조인하면(현재 backend dashboard.py 방식) 선석 점유
--   판정이 대부분 "여유"로 오표시된다(운항 기록 92%가 마스터와 안 붙음).
--
-- ★ 정규화만으로 해결되는 범위 — 두 함수로 66%(113종 중 75종) 자동 일치
--   norm_facility(): 괄호 안 부가정보 제거('용연부두(1선석)' → '용연부두')
--                     + 공백 제거 + 소문자화('S-OIL1부두'·'S-Oil 1부두' 동일화)
--   norm_berth():     위 + 끝자리 접미 선석번호 제거('SK2부두 01' → 'SK2부두')
--                      단, '정박지'는 예외 — 번호 자체가 정박지 식별자라서
--                      제거하면 '정박지 01'과 '04'가 뭉개진다.
--
-- ★ 정규화로 안 풀리는 39종 — 세 갈래로 분류(억지로 맞추지 않는다)
--   (a) 수동 별칭 12건 — 표기가 다를 뿐 같은 시설임을 확인한 것만
--       (OTK부두→OTK1부두는 확신도 낮음 — OTK1/2 중 하나로 통계상 추정.
--        운영 확인 전까지 참고용으로만 쓸 것)
--   (b) OTHER 14종 — 호안·물양장·의장안벽 등. 선석 제원 마스터에 원래 없는
--       시설이 정상이다(장생포호안 등은 접안 시설이 아니라 계류/작업 공간).
--       억지로 매핑하면 존재하지 않는 선석 점유를 만들어낸다.
--   (c) UNMAPPED 13종 — '정박지 01'~'07'은 Neo4j Anchorage(B1-1~W1 20종)의
--       어느 코드와 대응하는지 근거 자료가 없다(E1/E2/E3만 기존에 확인됨).
--       '현대오일터미널신항부두'도 마스터의 '신항1부두'/'신항2부두' 중 어느
--       쪽인지 표기만으로 판별 불가. 모르는 걸 안다고 하지 않는다 —
--       berth_draught_check의 UNKNOWN과 같은 원칙.
--
-- ★ 구체화(MATERIALIZED)하는 이유: upa_port_call 20,000+행을 매번 훑어
--   DISTINCT 를 뽑는 대신, 수집 시점에만 바뀌는 이 157종(실측 시점 기준)
--   사전을 한 번 구체화해 두고 조회는 상수 시간으로 만든다. dashboard_current
--   등 다른 뷰와 같은 이유(4-2절 성능 실측과 동일 원칙).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mart.norm_facility(text) RETURNS text AS $$
    SELECT lower(regexp_replace(regexp_replace($1, '\(.*\)', '', 'g'), '\s+', '', 'g'));
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION mart.norm_berth(text) RETURNS text AS $$
    SELECT CASE
        WHEN $1 ~ '^정박지' THEN mart.norm_facility($1)
        ELSE regexp_replace(mart.norm_facility($1), '[0-9]+$', '')
    END;
$$ LANGUAGE sql IMMUTABLE;

-- ★ 2026-08-11 개정 — 마스터를 upa_berth_facility로 교체
--   초판은 Neo4j Berth.wharf_name 목록을 이 파일에 VALUES로 손으로 옮겨 적어
--   대조했다. 문제는 Neo4j가 그 목록의 정본이 아니라는 것 — berth_neo4j_loader.py가
--   Neo4j Berth.wharf_name을 upa_berth_facility.wharf_name에서 그대로(가공 없이)
--   가져온다("wharf_name은 upa_berth_facility_stg.csv 실제 값과 정확히 일치해야
--   한다", 로더 주석). 즉 upa_berth_facility가 정본이고 Neo4j는 그 파생물이다.
--   여기서 손으로 옮긴 목록을 쓰면 (a) 정본이 둘로 갈라지고 (b) 선석이 추가·
--   개명돼도 이 파일을 다시 고쳐야 한다. Postgres 안에서 정본을 직접 참조하면
--   그 문제가 사라지고, 부수 효과로 facility_type='BERTH' 판정도 "실제 마스터에
--   있는지"로 검증된다(초판은 나머지 전부를 BERTH로 기본 처리해서, 규칙이
--   틀리게 붙여도 걸러낼 방법이 없었다).
CREATE MATERIALIZED VIEW mart.facility_alias AS
WITH master AS (
    -- 선석 제원 마스터(정본) — Neo4j Berth는 이 테이블의 파생물이다(위 설명 참고).
    SELECT DISTINCT wharf_name, mart.norm_berth(wharf_name) AS berth_key
    FROM upa_berth_facility
    WHERE wharf_name IS NOT NULL AND btrim(wharf_name) <> ''
),
manual_alias AS (
    -- 표기만 다를 뿐 같은 시설임을 확인한 것만. 새로 발견되면 여기 추가할 것 —
    -- 정규화 규칙을 늘리지 말 것(규칙을 늘리면 위 두 함수가 다른 시설까지
    -- 잘못 묶기 시작한다. 예외는 사전으로 관리하는 게 안전하다).
    -- target_name은 master.wharf_name과 정확히 일치해야 한다 — 안 맞으면
    -- 아래 조인에서 걸러져 UNMAPPED로 떨어진다(오탈자 방지 자체검증).
    SELECT * FROM (VALUES
        ('OTK부두',                'OTK1부두',        'BERTH'),
        ('엘에스니꼬신항부두',      'LS MNM 신항부두', 'BERTH'),
        ('신항컨테이너부두01',      '신항컨부두',       'BERTH'),
        ('신항컨테이너부두02',      '신항컨부두',       'BERTH'),
        ('신항컨테이너부두03',      '신항컨부두',       'BERTH'),
        ('신항컨테이너부두04',      '신항컨부두',       'BERTH'),
        ('용잠부두 01',            '용잠1부두',        'BERTH'),
        ('용잠부두 02',            '용잠2부두',        'BERTH'),
        ('SK부이 02',              'SK2부이',          'BERTH'),
        ('SK부이 03',              'SK3부이',          'BERTH'),
        -- [2026-09-22] '유화1부두'를 아래 other_facility(비선석)에서 여기로 옮겼다.
        --   근거: portmis_facility_map(MDW/01, facility_nm='유화1부두')이
        --   wharf_name='대한유화부두'로 이미 매핑돼 있고, 두 사전이 겹치는 70종
        --   중 이 한 줄만 서로 어긋나 있었다(69 일치 / 1 불일치, 실측).
        --   대조 근거: upa_berth_facility 에서 이름에 '유화'가 들어가는 시설은
        --   대한유화부두 하나뿐(온산항, 운영사 대한유화㈜, 12m, 320m)이고,
        --   upa_port_call 의 '유화1부두' 54건도 전부 온산항 건이다.
        --   PORT-MIS 코드(MDW)와 UPA 코드(MWD)가 뒤집혀 있어 코드로는 못 붙고
        --   이름으로만 붙는다 — 그래서 자동 규칙이 아니라 이 사전에 둔다.
        --   min=max=12m 라 선석 간 수심 차가 없어 WHARF 수준 매칭으로 충분하다.
        ('유화1부두',              '대한유화부두',     'BERTH'),
        ('정박지-E1',              'E1',               'ANCHORAGE'),
        ('정박지-E2',              'E2',               'ANCHORAGE'),
        ('정박지-E3',              'E3',               'ANCHORAGE')
    ) AS t(source_name, target_name, facility_type)
),
other_facility AS (
    -- 선석 제원 마스터에 원래 없는 시설 — 호안·물양장·의장안벽 등.
    SELECT unnest(ARRAY[
        '장생포호안', '이진물양장', '현중해양의장안벽',
        '현대미포의장안벽01', '현대미포의장안벽02', '현대미포의장안벽03',
        '현대미포의장안벽04', '현대미포의장안벽05',
        -- '유화1부두' 는 2026-09-22 에 manual_alias(대한유화부두)로 옮겼다 — 위 설명.
        '우봉물양장', '신항부두작업장', '세방신항부두',
        '매암부두', 'S-OIL 2부이'
    ]) AS source_name
),
source_names AS (
    -- upa_port_call(VTS 원문) + upa_cargo_manifest(합성 화물의 자체 표기) 합집합.
    -- (아래 사고 설명은 2026-09-17 전 정의 기준. 지금 berth_current_cargo.facility_name
    --  은 위치 판정 선석의 마스터 표기라 맨 아래 마스터 자신 항목으로 붙는다.)
    -- 둘이 서로 다른 어휘를 쓴다 — berth_current_cargo.facility_name은
    -- COALESCE(cm.facility_name, ip.facility_name)라(뷰 8번 정의) 화물이 매칭된
    -- 행은 합성 manifest 표기('S-Oil 1부두', 이미 정돈된 형태)로 나오고 화물이
    -- 없는 행만 VTS 원문('S-OIL1부두')으로 나온다. 이 사전이 한쪽만 알면
    -- berth_current_cargo를 조인하는 소비자(scheduling/service.py)가 매번
    -- 이중 정규화 폴백을 따로 구현해야 한다 — 실제로 그 사고가 났었다.
    SELECT DISTINCT facility_name AS source_name
    FROM upa_port_call
    WHERE facility_name IS NOT NULL AND btrim(facility_name) <> ''
    UNION
    SELECT DISTINCT facility_name
    FROM upa_cargo_manifest
    WHERE facility_name IS NOT NULL AND btrim(facility_name) <> ''
    UNION
    -- 마스터 표기 자신. mart.vessel_presence(2-1)는 좌표로 찾은 선석을 마스터
    -- 표기로 내보내는데, 'UTK 신항부두'·'신항컨부두' 같은 27종은 VTS·manifest
    -- 어느 쪽에도 그 표기로 나온 적이 없어(2026-09-17 실측) 이 사전을 거치는
    -- 소비자(scheduling/service.py)가 그 선석의 화물을 못 찾았다.
    SELECT DISTINCT wharf_name
    FROM upa_berth_facility
    WHERE wharf_name IS NOT NULL AND btrim(wharf_name) <> ''
),
matched AS (
    SELECT
        s.source_name,
        ma.facility_type          AS manual_type,
        mb.wharf_name             AS manual_wharf_name,   -- 수동 별칭이 실제 마스터에 있는지 검증됨
        ma.target_name            AS anchorage_key,        -- ANCHORAGE면 Neo4j Anchorage.id
        o.source_name IS NOT NULL AS is_other,
        m.wharf_name              AS auto_wharf_name       -- 정규화 규칙으로 마스터와 자동 매칭된 것
    FROM source_names s
    LEFT JOIN manual_alias ma ON ma.source_name = s.source_name
    LEFT JOIN master mb       ON mb.wharf_name = ma.target_name AND ma.facility_type = 'BERTH'
    LEFT JOIN other_facility o ON o.source_name = s.source_name
    LEFT JOIN master m        ON m.berth_key = mart.norm_berth(s.source_name)
)
SELECT
    source_name,
    -- wharf_name: 선석 제원 마스터의 정본 표기. 이제 backend가 Neo4j에서 wharf_name
    -- 목록을 매 요청마다 다시 넘길 필요가 없다 — 이 값이 이미 Neo4j Berth.wharf_name과
    -- 문자 그대로 같다(둘 다 upa_berth_facility에서 왔으므로).
    COALESCE(manual_wharf_name, auto_wharf_name)                            AS wharf_name,
    CASE WHEN manual_type = 'ANCHORAGE' THEN anchorage_key END              AS anchorage_key,
    CASE
        WHEN manual_type = 'ANCHORAGE'                    THEN 'ANCHORAGE'
        WHEN manual_type = 'BERTH' AND manual_wharf_name IS NOT NULL THEN 'BERTH'
        WHEN manual_type = 'BERTH'                        THEN 'UNMAPPED'  -- 별칭 오탈자 등 자체검증 실패
        WHEN is_other                                     THEN 'OTHER'
        WHEN auto_wharf_name IS NOT NULL                  THEN 'BERTH'
        ELSE 'UNMAPPED'  -- 정박지 01~07, 현대오일터미널신항부두(신항1/2 판별 불가) 등
    END                                                                     AS facility_type
FROM matched;

CREATE UNIQUE INDEX idx_facility_alias_source ON mart.facility_alias (source_name);

-- ---------------------------------------------------------------------------
-- 1. mart.vessel_identity — 선박 식별 마스터 (1척 = 1행)
--    위치(UPA)와 PORT-MIS 를 callsgn 으로 묶고, 식별 우선순위 imo → mmsi →
--    callsgn 에 따라 대표 키(vessel_key)를 만든다. 선종·액체화물선 여부는
--    공식 신고 데이터인 PORT-MIS 를 정본으로 삼는다.
--
--    [MMSI-First + Stateful Filling]  (callsgn 결측 31% 대응)
--    UPA getVslPstnInfo 는 AIS 동적신호(위치)를 기반으로 하고, callsgn 은
--    정적신호(Msg Type 5)에서 온다. 정적신호는 6분 주기라 스냅샷에 따라
--    비거나(간헐 결측), 아예 송출하지 않는 선박(관공선·소방정·순찰선·예부선
--    등 150척)도 있다. 따라서:
--
--      (a) 파티션 키를 callsgn 이 아니라 MMSI 로 둔다 → callsgn 이 한 번도
--          없는 선박도 뷰에서 사라지지 않는다 (MMSI-First).
--      (b) callsgn/IMO/선명은 "그 MMSI 의 가장 최근 non-null 값"을 끌어와
--          채운다 (Stateful Filling / Last Known Position).
--          first_value(...) OVER (PARTITION BY vessel_uid
--                                 ORDER BY (x IS NOT NULL) DESC, received_at_utc DESC)
--          → boolean 은 false < true 이므로 DESC 를 주면 non-null 행이 먼저
--            오고, 그중 가장 최신 행의 값이 선택된다.
--      (c) callsgn_source 로 출처를 표기한다:
--            'current' = 최신 관측에 실제로 실려온 값
--            'filled'  = 과거 신호에서 끌어온 값 (정적신호 일시 누락)
--            NULL      = 한 번도 받은 적 없음 (MMSI 로만 식별)
--
--    ★ 주의: 여기서 앞으로 끌어오는 것은 "정적 식별정보"(호출부호·IMO·선명)
--      뿐이다. 위치(위경도·SOG·COG)는 절대 채워 넣지 않는다. 떠난 배가 옛
--      좌표로 남아 있으면 안전관제상 오히려 위험하다. 위치는 계속
--      mart.vessel_latest_position 이 관측된 시각 그대로 보여준다.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.vessel_identity AS
WITH pos_src AS (
    SELECT
        -- 선박 고유키: MMSI 우선, MMSI 조차 없으면 callsgn 으로 대체
        COALESCE(mmsi::text, 'CS:' || upper(btrim(callsgn)))  AS vessel_uid,
        mmsi,
        nullif(upper(btrim(callsgn)), '')                     AS callsgn,
        imo_no::bigint                                        AS imo_no,
        nullif(btrim(vessel_name), '')                        AS vessel_name,
        received_at_utc
    FROM upa_vessel_position
    WHERE mmsi IS NOT NULL
       OR nullif(btrim(callsgn), '') IS NOT NULL
),
pos_filled AS (
    SELECT
        vessel_uid,
        mmsi,
        received_at_utc,
        callsgn                                        AS callsgn_observed,
        first_value(callsgn)     OVER w_callsgn        AS callsgn_lkp,
        first_value(imo_no)      OVER w_imo            AS imo_no_lkp,
        first_value(vessel_name) OVER w_name           AS vessel_name_lkp
    FROM pos_src
    WINDOW
        w_callsgn AS (PARTITION BY vessel_uid
                      ORDER BY (callsgn     IS NOT NULL) DESC, received_at_utc DESC),
        w_imo     AS (PARTITION BY vessel_uid
                      ORDER BY (imo_no      IS NOT NULL) DESC, received_at_utc DESC),
        w_name    AS (PARTITION BY vessel_uid
                      ORDER BY (vessel_name IS NOT NULL) DESC, received_at_utc DESC)
),
pos_latest AS (
    -- vessel_uid 당 1행: 시각은 "최신 관측 시각", 식별정보는 위에서 채운 값
    SELECT DISTINCT ON (vessel_uid)
           vessel_uid,
           callsgn_lkp                     AS callsgn,
           mmsi,
           imo_no_lkp                      AS imo_no,
           vessel_name_lkp                 AS vessel_name,
           received_at_utc                 AS last_position_at_utc,
           CASE
               WHEN callsgn_observed IS NOT NULL THEN 'current'
               WHEN callsgn_lkp      IS NOT NULL THEN 'filled'
               ELSE NULL
           END                             AS callsgn_source
    FROM pos_filled
    ORDER BY vessel_uid, received_at_utc DESC
),
pm_latest AS (
    SELECT DISTINCT ON (upper(btrim(callsgn)))
           upper(btrim(callsgn))           AS callsgn,
           vessel_name                     AS vessel_name_portmis,
           nationality_cd,
           nationality_nm,
           ship_kind_cd,
           ship_kind_nm,
           ship_kind_category,
           is_liquid_cargo_vessel,
           port_agency_label
    FROM portmis_vessel
    WHERE nullif(btrim(callsgn), '') IS NOT NULL
    ORDER BY upper(btrim(callsgn)), collected_at_utc DESC
),
base AS (
    -- ① 위치 신호가 있는 선박 (callsgn 유무와 무관하게 전부 포함)
    SELECT vessel_uid, callsgn, mmsi, imo_no, vessel_name,
           last_position_at_utc, callsgn_source, TRUE AS has_position
    FROM pos_latest

    UNION ALL

    -- ② PORT-MIS 신고만 있고 아직 항내 위치가 안 잡힌 선박 (입항 예정 등)
    SELECT 'CS:' || m.callsgn, m.callsgn, NULL::bigint, NULL::bigint, NULL::text,
           NULL::timestamptz, 'portmis'::text, FALSE
    FROM pm_latest m
    WHERE NOT EXISTS (SELECT 1 FROM pos_latest p WHERE p.callsgn = m.callsgn)
)
SELECT
    -- 식별 우선순위: imo → mmsi → callsgn
    COALESCE(b.imo_no::text, b.mmsi::text, b.callsgn)   AS vessel_key,
    b.callsgn,
    b.imo_no,
    b.mmsi,
    COALESCE(b.vessel_name, m.vessel_name_portmis)      AS vessel_name,
    m.nationality_cd,
    m.nationality_nm,
    m.ship_kind_cd,
    m.ship_kind_nm,
    m.ship_kind_category,
    m.is_liquid_cargo_vessel,
    m.port_agency_label,
    b.last_position_at_utc,
    b.has_position,
    (m.callsgn IS NOT NULL)                             AS has_portmis,
    -- ↓ 신규 컬럼 (CREATE OR REPLACE 제약상 반드시 맨 뒤에 추가할 것)
    b.callsgn_source,
    b.vessel_uid,
    -- -----------------------------------------------------------------------
    -- identity_confidence — 식별 신뢰도
    --
    -- ★ "정적신호가 없는 선박 = 안전한 선박" 이 아니다.
    --   울산·온산 같은 액체화물 허브에는 대형 유조선 옆에 붙어 화물을 옮겨 싣는
    --   소형 급유선(bunker vessel), 액체 부선(barge), 300GT 미만 연안 케미칼선이
    --   상시 움직인다. 이들은 Class B 를 쓰거나 AIS 의무 대상이 아니어서 선종이
    --   확인되지 않는다. 이 선박들을 "액체화물선 아님"으로 단정해 목록에서
    --   지워버리면, 실제로는 위험물을 실은 배가 관제 화면에서 사라진다.
    --
    --   그래서 판정을 "액체/비액체" 2값이 아니라 아래 3상태로 남긴다.
    --   소비 계층(대시보드·에이전트)은 UNIDENTIFIED 를 '안전'이 아니라
    --   '미확인 위험'으로 취급하고, 관제사에게 VTS 교신 확인을 유도해야 한다.
    --
    --     CONFIRMED    PORT-MIS 입출항신고로 선종이 확정됨 (정본)
    --     PARTIAL      신고는 없으나 정적신호(callsgn/IMO)로 식별은 됨
    --     UNIDENTIFIED 정적신호 수신 이력 자체가 없음 — MMSI 만 아는 상태
    -- -----------------------------------------------------------------------
    CASE
        WHEN m.callsgn IS NOT NULL                     THEN 'CONFIRMED'
        WHEN b.callsgn IS NOT NULL OR b.imo_no IS NOT NULL THEN 'PARTIAL'
        ELSE 'UNIDENTIFIED'
    END                                                 AS identity_confidence
FROM base b
LEFT JOIN pm_latest m ON m.callsgn = b.callsgn;

-- ---------------------------------------------------------------------------
-- 2. mart.vessel_latest_position — 선박별 최신 위치 1행
--    UPA 항내 선박위치 단일 소스 (2026-09-17 레거시 AIS 합치기 제거 — 아래 unified
--    주석). position_source 컬럼은 소비자 호환을 위해 'UPA' 로 남겨 둔다.
--
--    [MMSI-First] 예전에는 callsgn 없는 UPA 관측을 통째로 버려서 관공선·
--    소방정·순찰선 등이 지도에서 사라졌다. 이제 MMSI 를 1차 키로 쓰고
--    callsgn 은 vessel_identity 에서 역매핑해 보강만 한다.
--    ★ 위치값 자체는 Stateful Filling 대상이 아니다. 신호가 끊긴 배는 마지막
--      관측 시각(received_at_utc)과 함께 그대로 남고, 좌표를 만들어내지
--      않는다. 소비 측은 now() - received_at_utc 로 신선도를 판단할 것.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.vessel_latest_position AS
-- MATERIALIZED: AIS 합치기(UNION ALL)를 뺀 뒤 플래너가 이 CTE 를 바깥
-- dashboard_current 조인 안으로 풀어 넣으면서 /vessels 조회가 1.5초 → 4초로
-- 느려졌다(2026-09-17 실측). 한 번 계산해 두게 하면 1.1초다.
WITH unified AS MATERIALIZED (
    SELECT COALESCE(mmsi::text, 'CS:' || upper(btrim(callsgn))) AS vessel_uid,
           nullif(upper(btrim(callsgn)), '')  AS callsgn,
           mmsi,
           latitude,
           longitude,
           -- AIS 합치기가 있을 때 타입이 double precision 으로 올라가 있었다.
           -- 소비자 계약(JSON 실수)을 그대로 두려고 명시적으로 맞춘다.
           sog::double precision      AS sog,
           cog::double precision      AS cog,
           heading::double precision  AS heading,
           draught,
           nav_status_code::text      AS nav_status_code,
           received_at_utc,
           quality_flag,
           'UPA'                      AS position_source
    FROM upa_vessel_position
    WHERE mmsi IS NOT NULL
       OR nullif(btrim(callsgn), '') IS NOT NULL
    -- ★ 2026-09-17 — 레거시 AIS(ais_vessel_position) 합치기를 뺐다.
    --   aisstream 수신은 멈췄고(1,112행, LEGACY_DOMAINS) 그 행은 received_at_utc 가
    --   전부 NULL 이었다. 아래 DISTINCT ON ... ORDER BY received_at_utc DESC 에서
    --   NULL 이 맨 앞에 오므로(DESC 기본값 NULLS FIRST) 411척이 지금 UPA 위치 대신
    --   옛 AIS 좌표(부산 앞바다 등)·"신호 없음"으로 보였다 — 온산 선석에 접안해
    --   있는 배 6척이 지도·KPI 에서 빠진 것도 이 때문이었다.
    --   선박 위치의 정본은 UPA 선박위치 하나다(메모: vessel-state-truth).
),
-- 서류상 재항 여부 — 출항은 "추정"하지 않고 upa_port_call 로 "확정"한다.
-- departure_at_utc 가 NULL 인 최신 입항 건이 있으면 아직 항내에 있다는 신고 상태.
still_in_port AS (
    SELECT DISTINCT upper(btrim(callsgn)) AS callsgn
    FROM upa_port_call
    WHERE nullif(btrim(callsgn), '') IS NOT NULL
      AND departure_at_utc IS NULL
),
departed_doc AS (
    SELECT upper(btrim(callsgn)) AS callsgn, max(departure_at_utc) AS departed_at_utc
    FROM upa_port_call
    WHERE nullif(btrim(callsgn), '') IS NOT NULL
      AND departure_at_utc IS NOT NULL
    GROUP BY 1
),
latest AS (
    SELECT DISTINCT ON (u.vessel_uid)
           u.vessel_uid                          AS vessel_pos_key,
           -- 관측에 callsgn 이 안 실렸으면 vessel_identity 의 Last Known 값으로 보강
           COALESCE(u.callsgn, vi2.callsgn)      AS callsgn,
           u.mmsi,
           u.latitude,
           u.longitude,
           u.sog,
           u.cog,
           u.heading,
           u.draught,
           u.nav_status_code,
           u.received_at_utc,
           u.quality_flag,
           u.position_source
    FROM unified u
    LEFT JOIN mart.vessel_identity vi2 ON vi2.vessel_uid = u.vessel_uid
    ORDER BY u.vessel_uid, u.received_at_utc DESC NULLS LAST
)
SELECT l.*,
       -- ↓ 신규 컬럼 (CREATE OR REPLACE 제약상 반드시 맨 뒤에 추가할 것)
       -- -----------------------------------------------------------------------
       -- 원시 신선도. 임계값을 박아 넣지 않은 값이라 소비자가 각자 기준을
       -- 적용할 수 있다 (지도는 30분, 안전 에이전트는 더 엄격하게).
       -- -----------------------------------------------------------------------
       round(EXTRACT(EPOCH FROM (now() - l.received_at_utc)) / 60.0)::int
                                                 AS position_age_min,
       -- -----------------------------------------------------------------------
       -- presence_state — 화면 표시용 존재 상태
       --
       -- ★ "신호가 없다"와 "배가 떠났다"는 다른 사건이다.
       --   6시간 침묵의 원인은 실제 출항 / 접안 중 AIS 차단 / 트랜스폰더 고장 /
       --   구조물 전파음영 / 우리 수집기 장애 최소 5가지다. 이걸 전부
       --   DEPARTED 로 부르고 화면에서 지우면, 트랜스폰더가 죽은 채 선석에
       --   붙어 있는 배가 지도에서 사라진다. 안전관제에서 이건 결함이다.
       --
       --   그래서 출항은 신호 부재로 추론하지 않고 upa_port_call 의
       --   departure_at_utc(서류상 확정 정보)로만 판정한다.
       --
       --     PRESENT    30분 이내 관측 — 정상 표시
       --     STALE      30분~6시간 침묵 — 회색 반투명, "N분 전" 배지
       --     NO_SIGNAL  6시간 초과 침묵 + 출항 기록 없음
       --                → ★숨기지 말 것. 별도 목록으로 관제사에게 노출.
       --                  (서류상 재항 + 신호 소실 = 전화를 걸어야 하는 상황)
       --     DEPARTED   출항 신고 확정 — 지도에서 제외해도 안전
       --
       --   임계값 30분/6시간은 현재 실측 근거가 없는 잠정값이다.
       --   mart_views_check.sql 의 관측간격 분위수 쿼리로 재산정할 것.
       -- -----------------------------------------------------------------------
       CASE
           WHEN d.departed_at_utc IS NOT NULL
                AND d.departed_at_utc >= l.received_at_utc      THEN 'DEPARTED'
           WHEN l.received_at_utc > now() - interval '30 min'    THEN 'PRESENT'
           WHEN l.received_at_utc > now() - interval '6 hour'    THEN 'STALE'
           ELSE 'NO_SIGNAL'
       END                                       AS presence_state,
       -- 서류상 아직 항내인가 (출항신고 없는 입항 건 보유)
       (s.callsgn IS NOT NULL)                   AS doc_still_in_port,
       d.departed_at_utc,
       -- -----------------------------------------------------------------------
       -- signal_health — 안전 에이전트용. 지도 표시보다 엄격하다.
       -- 하역 중인 배가 12분(6분 폴링 2회분) 침묵하면 이미 이상 상황이다.
       -- 지도 기준(30분)으로는 그 29분 동안 정상 아이콘으로 보인다.
       -- -----------------------------------------------------------------------
       CASE
           WHEN l.received_at_utc > now() - interval '12 min'    THEN 'OK'
           WHEN l.received_at_utc > now() - interval '30 min'    THEN 'DEGRADED'
           ELSE 'LOST'
       END                                       AS signal_health
FROM latest l
LEFT JOIN still_in_port s ON s.callsgn = l.callsgn
LEFT JOIN departed_doc  d ON d.callsgn = l.callsgn;

-- ---------------------------------------------------------------------------
-- 2-1. mart.vessel_presence — 지금 선석·정박지에 "실제로 있는" 배 (선박당 1행)
--
-- ★ 왜 위치로 판정하나 — upa_port_call 은 "신고 이벤트"다 (2026-09-17 실측)
--   (a) 유령: 출항 처리가 빠진 건이 남는다. "입항했고 출항 기록 없음"으로 세면
--       선석 58곳에 818척이 점유 중이었고, 그중 775척은 입항한 지 7일이 넘었다.
--   (b) 구간이 넓다: 입항~출항에는 정박지 대기·이선이 다 들어 있다. 최근 40일
--       완료 입항 건의 체류 중 정지 위치 4,373개 중 47%가 신고 선석에서 2km
--       밖이었다(대부분 정박(앵커링)). 이 구간으로 세면 정박지에 있는 배가
--       선석을 점유한 것으로 나온다.
--   (c) 입항 건 하나가 이벤트 여러 행(입항·접안·이선·이안·투묘·양묘·출항)이고
--       행마다 시설이 다르다. DISTINCT ON (port_call_id) ORDER BY arrival_at_utc
--       는 arrival 이 모든 행에서 같아서 아무 이벤트의 시설이나 집는다.
--   → "지금 어디 있나"는 UPA 선박위치로 판정하고, port_call 은 "어느 선석으로
--     신고했나"라는 이름표(배마다 최신 이벤트 1행)로만 쓴다.
--     port_call 의 이력·통계 용도(berth_dwell_stats, 백테스트)는 그대로 둔다.
--
-- ★ 판정 규칙
--   대상      선박별 최신 UPA 위치 1행. 최신 수집 시각에서 3시간 이내만(EC2 수집
--             1시간 주기). now() 가 아니라 최신 수집 시각 기준이라 수집이 멈춰도
--             화면이 "빈 항만"이 되지 않고 마지막 스냅샷을 보여준다 — 그 시각은
--             snapshot_at_utc 로 함께 내보내고, 수집 정지 경고는 pipeline_health 몫.
--   BERTH     정지(sog ≤ 0.5kn) · 정박(앵커링) 아님 · VTS 입항 기록이 있는 배이고
--             ① 최신 VTS 이벤트(입항·접안·이선)의 선석이 1km 안 → 그 선석 ('신고+위치')
--             ② 아니면 가장 가까운 선석이 300m 안             → 그 선석 ('위치')
--             ③ 신고 선석에 좌표가 없고(부이 등) 정박지 구역 밖  → 신고 선석 ('신고')
--   ANCHORAGE BERTH 가 아니고, 정지 또는 정박(앵커링)이며 정박지 구역(upa_anchorage) 안
--   STOPPED   그 밖의 정지·묘박 — 조선소 의장안벽·물양장·예인선 대기 등
--   UNDERWAY  그 밖
--
-- ★ 임계값 근거 (최근 40일 완료 입항 건의 체류 중 정지 위치 실측, 2026-09-17)
--   - 신고 선석 좌표까지 1km 안 2,143개 중 95%가 600m 안. 선석 좌표가 선석당 한
--     점이고 SK5부두(798m)·6부두(990m)처럼 긴 안벽이 있어 1km 로 둔다.
--   - 가장 가까운 선석이 신고 선석과 같은 비율은 300m 안에서도 64%뿐이다. 이웃
--     선석 간격이 200~400m 라 좌표만으로는 옆 선석과 헷갈린다 → 신고 선석을 먼저
--     보고, 가장 가까운 선석은 신고가 없거나 1km 밖일 때만 쓴다(berth_basis 로 구분).
--     용잠1·2부두는 원천 좌표가 똑같아 ②로는 구분되지 않는다(이름순 첫 번째).
--   - "VTS 입항 기록이 있는 배" 조건: 예인선·급유선·관공선 수십 척이 부두 근처에
--     모여 정지해 있어, 이 조건이 없으면 상선 선석 점유가 부풀었다.
--   - 좌표가 없는 선석 9곳(부이 4기·북신항 등)은 거리로 확인할 수 없어 ③처럼 신고에
--     기댄다. 그래도 "지금 위치가 새로 들어오고 정지해 있다"는 조건은 같이 걸려
--     유령 기록은 걸러진다. 신고도 없는 배가 그곳에 있으면 모른다.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mart.dist_m(double precision, double precision,
                                       double precision, double precision)
RETURNS double precision AS $$
    -- 하버사인 거리(m). 인자 순서: 위도1, 경도1, 위도2, 경도2
    SELECT 6371000 * 2 * asin(sqrt(
        power(sin(radians($3 - $1) / 2), 2)
        + cos(radians($1)) * cos(radians($3)) * power(sin(radians($4 - $2) / 2), 2)));
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE VIEW mart.vessel_presence AS
WITH snap AS (
    SELECT max(received_at_utc) AS at FROM upa_vessel_position
),
pos AS (
    SELECT DISTINCT ON (v.vessel_uid)
           v.vessel_uid,
           nullif(upper(btrim(v.callsgn)), '') AS callsgn_obs,
           v.mmsi, v.vessel_name, v.latitude, v.longitude, v.sog,
           v.nav_status_code, v.draught, v.received_at_utc
    FROM upa_vessel_position v
    CROSS JOIN snap
    WHERE v.received_at_utc > snap.at - interval '3 hours'
      AND v.latitude IS NOT NULL AND v.longitude IS NOT NULL
    ORDER BY v.vessel_uid, v.received_at_utc DESC
),
cs AS (
    -- 최신 관측에 호출부호가 빠진 배(정적신호 간헐 결측)는 같은 배의 마지막 값으로 보강.
    -- 지금 보이는 배로만 좁혀 훑는다(전체 정렬은 조회마다 0.1초 이상).
    SELECT DISTINCT ON (vessel_uid) vessel_uid, upper(btrim(callsgn)) AS callsgn
    FROM upa_vessel_position
    WHERE nullif(btrim(callsgn), '') IS NOT NULL
      AND vessel_uid IN (SELECT vessel_uid FROM pos)
    ORDER BY vessel_uid, received_at_utc DESC
),
ident AS (
    SELECT p.*, COALESCE(p.callsgn_obs, cs.callsgn) AS callsgn
    FROM pos p
    LEFT JOIN cs ON cs.vessel_uid = p.vessel_uid
),
berth AS (
    SELECT DISTINCT ON (wharf_name) wharf_name, latitude, longitude
    FROM upa_berth_facility
    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    ORDER BY wharf_name, collected_at_utc DESC NULLS LAST
),
anch_pt AS (
    -- POLYGON 은 경계 정점 여러 개, CIRCLE·BUNKER_RING 은 중심 1점 + 공시 반경.
    -- TEXT 는 해도 글자 위치라 뺀다.
    SELECT anchorage_name, latitude, longitude, radius_m
    FROM upa_anchorage
    WHERE anchorage_type IN ('POLYGON', 'CIRCLE', 'BUNKER_RING')
      AND latitude IS NOT NULL AND longitude IS NOT NULL
),
anch_center AS (
    SELECT anchorage_name, avg(latitude) AS lat, avg(longitude) AS lon
    FROM anch_pt
    GROUP BY anchorage_name
),
anch AS (
    -- PostGIS 가 없어 다각형 내부 판정 대신 "중심 → 가장 먼 정점" 원으로 근사하고,
    -- 원형은 공시 반경을 쓴다. 둘 다 +200m(묘박 선회 여유).
    SELECT c.anchorage_name, c.lat, c.lon,
           CASE WHEN max(p.radius_m) > 0 THEN max(p.radius_m)
                ELSE max(mart.dist_m(c.lat, c.lon, p.latitude, p.longitude))
           END + 200 AS radius_m
    FROM anch_center c
    JOIN anch_pt p USING (anchorage_name)
    GROUP BY c.anchorage_name, c.lat, c.lon
),
vts_last AS (
    -- 배마다 최신 VTS 이벤트 1행 — 신고 선석 이름표와 입항 시각
    SELECT DISTINCT ON (upper(btrim(p.callsgn)))
           upper(btrim(p.callsgn)) AS callsgn,
           p.io_vts_name, p.facility_name, p.job_at_utc, p.arrival_at_utc,
           fa.facility_type, fa.wharf_name
    FROM upa_port_call p
    LEFT JOIN mart.facility_alias fa ON fa.source_name = p.facility_name
    WHERE upper(btrim(p.callsgn)) IN (SELECT callsgn FROM ident WHERE callsgn IS NOT NULL)
    ORDER BY upper(btrim(p.callsgn)), p.job_at_utc DESC NULLS LAST, p.comm_count DESC NULLS LAST
),
located AS (
    SELECT i.vessel_uid, i.callsgn, i.mmsi, i.vessel_name, i.latitude, i.longitude, i.sog,
           i.nav_status_code, i.draught, i.received_at_utc,
           vl.io_vts_name, vl.facility_name, vl.facility_type, vl.job_at_utc, vl.arrival_at_utc,
           CASE WHEN vl.io_vts_name IN ('입항', '접안', '이선') AND vl.facility_type = 'BERTH'
                THEN vl.wharf_name END                      AS declared_berth,
           (vl.callsgn IS NOT NULL)                         AS has_vts_record,
           COALESCE(i.sog <= 0.5, false)                    AS is_stopped
    FROM ident i
    LEFT JOIN vts_last vl ON vl.callsgn = i.callsgn
),
measured AS (
    SELECT l.*,
           mart.dist_m(l.latitude, l.longitude, db.latitude, db.longitude) AS declared_dist_m,
           (l.declared_berth IS NOT NULL AND db.wharf_name IS NULL) AS declared_no_coord,
           nb.wharf_name     AS nearest_berth,
           nb.dist_m         AS nearest_dist_m,
           na.anchorage_name AS nearest_anchorage,
           na.dist_m         AS anchorage_dist_m,
           na.radius_m       AS anchorage_radius_m
    FROM located l
    LEFT JOIN berth db ON db.wharf_name = l.declared_berth
    LEFT JOIN LATERAL (
        SELECT b.wharf_name, mart.dist_m(l.latitude, l.longitude, b.latitude, b.longitude) AS dist_m
        FROM berth b
        ORDER BY mart.dist_m(l.latitude, l.longitude, b.latitude, b.longitude), b.wharf_name
        LIMIT 1
    ) nb ON true
    LEFT JOIN LATERAL (
        -- 구역 반경 대비 가장 "안쪽"인 정박지
        SELECT a.anchorage_name, a.radius_m, mart.dist_m(l.latitude, l.longitude, a.lat, a.lon) AS dist_m
        FROM anch a
        ORDER BY mart.dist_m(l.latitude, l.longitude, a.lat, a.lon) / a.radius_m
        LIMIT 1
    ) na ON true
),
judged AS (
    SELECT m.*,
           CASE
               WHEN NOT (m.is_stopped AND m.has_vts_record)
                    OR m.nav_status_code = '정박(앵커링)'   THEN NULL
               WHEN m.declared_dist_m <= 1000               THEN '신고+위치'
               WHEN m.nearest_dist_m <= 300                 THEN '위치'
               WHEN m.declared_no_coord
                    AND NOT COALESCE(m.anchorage_dist_m <= m.anchorage_radius_m, false)
                                                            THEN '신고'
           END AS berth_basis
    FROM measured m
)
SELECT
    j.vessel_uid,
    j.callsgn,
    j.mmsi,
    j.vessel_name,
    CASE
        WHEN j.berth_basis IS NOT NULL                                  THEN 'BERTH'
        WHEN (j.is_stopped OR j.nav_status_code = '정박(앵커링)')
             AND j.anchorage_dist_m <= j.anchorage_radius_m             THEN 'ANCHORAGE'
        WHEN j.is_stopped OR j.nav_status_code = '정박(앵커링)'          THEN 'STOPPED'
        ELSE 'UNDERWAY'
    END                                                   AS presence_zone,
    CASE j.berth_basis
        WHEN '위치' THEN j.nearest_berth
        ELSE j.declared_berth
    END                                                   AS berth_name,
    j.berth_basis,
    round(CASE j.berth_basis
              WHEN '신고+위치' THEN j.declared_dist_m
              WHEN '위치'      THEN j.nearest_dist_m
          END)::int                                       AS berth_dist_m,
    CASE WHEN j.berth_basis IS NULL
              AND (j.is_stopped OR j.nav_status_code = '정박(앵커링)')
              AND j.anchorage_dist_m <= j.anchorage_radius_m
         THEN j.nearest_anchorage END                     AS anchorage_name,
    j.latitude,
    j.longitude,
    j.sog,
    j.nav_status_code,
    j.draught,
    j.received_at_utc,
    -- 이름표로 쓴 VTS 최신 이벤트 (판정 근거를 화면·보고서가 그대로 보여줄 수 있게)
    j.io_vts_name                                         AS vts_event,
    j.facility_name                                       AS vts_facility_name,
    j.facility_type                                       AS vts_facility_type,
    j.job_at_utc                                          AS vts_event_at_utc,
    j.arrival_at_utc                                      AS vts_arrival_at_utc,
    (SELECT at FROM snap)                                 AS snapshot_at_utc,
    round(EXTRACT(EPOCH FROM (now() - j.received_at_utc)) / 60.0)::int
                                                          AS position_age_min,
    -- [2026-09-22] 신선도 등급. position_age_min 만으론 화면이 임계값을 다시
    -- 정해야 해서, 판정과 같은 곳에서 등급까지 매긴다. UPA 선박위치 수집 주기가
    -- 10분이라 12분까지는 정상, 30분을 넘으면 한 번 이상 건너뛴 것이다.
    --
    -- ★ 이 뷰의 점유 판정 자체는 now() 가 아니라 최신 스냅샷(snap) 기준이다.
    --   수집이 멈춰도 "마지막으로 본 상태"는 계속 보여주고, 그게 얼마나 낡았는지는
    --   이 컬럼으로 밝힌다. now() 기준으로 판정하면 수집이 끊긴 순간 화면이
    --   통째로 비어 관제사가 아무것도 못 본다.
    CASE
        WHEN j.received_at_utc > now() - interval '12 minutes' THEN 'OK'
        WHEN j.received_at_utc > now() - interval '30 minutes' THEN 'DEGRADED'
        WHEN j.received_at_utc > now() - interval '6 hours'    THEN 'STALE'
        ELSE 'NO_SIGNAL'
    END                                                   AS quality_flag
FROM judged j;

-- ---------------------------------------------------------------------------
-- 3. mart.port_call_overview — 입항 통합 (입항건당 대표 1행)
--    PORT-MIS 입출항 신고 + UPA 운항관제(접안 부두·일시) + UPA 화물 manifest
--    요약(품목 수 / B/L 수 / 위험물 UN 번호)을 callsgn 으로 결합한다.
--
-- ★ 2026-08-20 수정 — portmis_vessel 미등재 선박의 VTS 실측 입출항시각 유실
--   이전엔 FROM pm_latest(portmis_vessel) 기준으로 pc_latest(upa_port_call)를
--   LEFT JOIN했다. portmis_vessel은 96행뿐인데 VTS(upa_port_call)에는 실제
--   입항 기록이 있는 선박이 572척이나 더 있어서(실측 확인, 2026-08-20), 그
--   572척은 이 뷰에 아예 행이 안 생겨 arrival_at_utc가 NULL로 나갔다 — VTS에
--   입항시각이 멀쩡히 있는데도 dashboard_current/port_call_overview 소비자
--   (arrival_watcher, orchestrator.py assess-and-commit 등)에게는 "모름"으로
--   보였다. 그 결과 관제사가 콘솔에서 승인한 배정의 actual_berthing_at이 거의
--   항상 NULL로 남고, 화면은 그 대신 planned_window(콘솔이 항상 "지금"으로
--   보내는 계획값)를 "(예정)"으로 표시해 "입항시간이 전부 현재시각"처럼 보였다.
--   FULL OUTER JOIN으로 바꿔 VTS만 있는 선박도 자기 행을 갖게 한다 — PORT-MIS
--   신고 필드(entry_purpose_nm 등)는 없으면 그냥 NULL(추측하지 않음).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.port_call_overview AS
WITH pm_latest AS (
    SELECT DISTINCT ON (upper(trim(callsgn)))
           upper(trim(callsgn))            AS callsgn,
           entry_year,
           entry_count,
           entry_purpose_cd,
           entry_purpose_nm,
           origin_port_nm,
           prev_port_nm,
           next_port_nm,
           dest_port_nm,
           is_domestic_voyage,
           port_agency_label
    FROM portmis_vessel
    WHERE callsgn IS NOT NULL
    ORDER BY upper(trim(callsgn)), collected_at_utc DESC
),
pc_latest AS (
    SELECT DISTINCT ON (upper(trim(callsgn)))
           upper(trim(callsgn))            AS callsgn,
           port_call_id,
           arrival_at_utc,
           departure_at_utc,
           facility_name,
           io_vts_name
    FROM upa_port_call
    WHERE callsgn IS NOT NULL
    ORDER BY upper(trim(callsgn)), arrival_at_utc DESC
),
-- 화물은 '최신 항차' 것만 센다.
--
-- 예전에는 callsgn 하나로만 GROUP BY 해서 그 배의 모든 항차 화물을 합산했다.
-- 바로 위 pm_latest·pc_latest 는 DISTINCT ON 으로 최신 입항 1건만 남기는데,
-- 화물만 과거 항차까지 합쳐 그 1건에 붙는 구조였다.
-- 실측(2026-08-15): 항차가 2개 이상인 선박 280척, 총 848항차, 최대 6항차.
--
-- 결과적으로 dashboard_current.dg_un_nos 를 통해 **지금 싣고 있지 않은 위험물**이
-- 관제 화면에 표시됐다. 혼재 판정의 입력이 되는 값이라 그냥 두면 없는 위험을
-- 만들어내는 셈이다.
--
-- 항차 자연키(ptent_yr, voyage_no)는 두 컬럼 모두 이미 적재되어 있다(bigint).
latest_voyage AS (
    SELECT DISTINCT ON (upper(trim(callsgn)))
           upper(trim(callsgn)) AS callsgn, ptent_yr, voyage_no
    FROM upa_cargo_manifest
    WHERE callsgn IS NOT NULL
    ORDER BY upper(trim(callsgn)), ptent_yr DESC NULLS LAST, voyage_no DESC NULLS LAST
),
cargo_sum AS (
    SELECT upper(trim(cm.callsgn))         AS callsgn,
           count(*)                        AS cargo_item_count,
           count(DISTINCT cm.bl_no)        AS bl_count,
           count(*) FILTER (WHERE cm.dg_un_no IS NOT NULL)     AS dg_cargo_count,
           string_agg(DISTINCT cm.dg_un_no::text, ',')         AS dg_un_nos,
           string_agg(DISTINCT cm.cargo_name_raw, ' | ')       AS cargo_names
    FROM upa_cargo_manifest cm
    JOIN latest_voyage lv
      ON lv.callsgn = upper(trim(cm.callsgn))
     AND lv.ptent_yr IS NOT DISTINCT FROM cm.ptent_yr
     AND lv.voyage_no IS NOT DISTINCT FROM cm.voyage_no
    WHERE cm.callsgn IS NOT NULL
    GROUP BY upper(trim(cm.callsgn))
),
combined AS (
    -- VTS(upa_port_call) 실측만 있고 PORT-MIS 신고가 없는 선박도 자기 행을
    -- 갖도록 FULL OUTER JOIN — callsgn은 두 쪽 다 없을 수 없으므로(WHERE로
    -- 이미 NULL 제외) COALESCE로 하나의 식별 키로 합친다.
    SELECT COALESCE(pm.callsgn, pc.callsgn)    AS callsgn,
           pm.entry_year,
           pm.entry_count,
           pm.entry_purpose_nm,
           pm.origin_port_nm,
           pm.prev_port_nm,
           pm.next_port_nm,
           pm.dest_port_nm,
           pm.is_domestic_voyage,
           pm.port_agency_label,
           pc.port_call_id,
           pc.arrival_at_utc,
           pc.departure_at_utc,
           pc.facility_name,
           pc.io_vts_name
    FROM pm_latest pm
    FULL OUTER JOIN pc_latest pc ON pc.callsgn = pm.callsgn
)
SELECT
    combined.callsgn,
    combined.entry_year,
    combined.entry_count,
    combined.entry_purpose_nm,
    combined.origin_port_nm,
    combined.prev_port_nm,
    combined.next_port_nm,
    combined.dest_port_nm,
    combined.is_domestic_voyage,
    combined.port_agency_label,
    combined.port_call_id,
    combined.arrival_at_utc,
    combined.departure_at_utc,
    combined.facility_name,
    combined.io_vts_name,
    cs.cargo_item_count,
    cs.bl_count,
    cs.dg_cargo_count,
    cs.dg_un_nos,
    cs.cargo_names
FROM combined
LEFT JOIN cargo_sum cs ON cs.callsgn = combined.callsgn;

-- ---------------------------------------------------------------------------
-- 3-1. mart.msds_flat — msds_chemical(JSONB) → 안전관제용 평면 뷰
--
--   스키마 합의(2026-07): msds_chemical 은 KOSHA 원본 16개 섹션을
--   msds_payload(JSONB)에 무손실 보존한다(로더 설계 원칙 유지). 대신 소비자
--   (mart 뷰·백엔드 API·LLM 에이전트)가 각자 JSONB 경로를 파고들면 파싱 규칙이
--   곳곳에 중복·분기되므로, **평탄화 규칙의 단일 정본**을 이 뷰 한 곳에 둔다.
--   → 원본 보존(JSONB)과 소비 편의(평면 컬럼)를 둘 다 취하는 구조.
--   → 필요한 항목이 늘면 이 뷰만 고치면 된다 (Alembic 마이그레이션 불필요).
--
--   추출 키는 KOSHA 가 부여하는 msdsItemCode(안정 코드)를 쓴다 — 한글 라벨이
--   바뀌어도 깨지지 않는다.
--     I14=인화점  I32=자연발화온도  I26=증기밀도
--     N02=UN번호  N06=운송위험성등급(IMDG)  N08=용기등급
--     B02=유해성분류  B0402=그림문자  B0404=신호어  B0406=유해·위험문구(H)
--
--   전제: msds_chemical 테이블은 backend(Alembic) 소유이며 이미 존재한다.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.msds_flat AS
WITH item AS (
    SELECT m.chem_id,
           it->>'msdsItemCode' AS item_code,
           it->>'itemDetail'   AS item_detail
    FROM msds_chemical m
    CROSS JOIN LATERAL jsonb_each(m.msds_payload) AS d(key, val)
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(d.val->'data') = 'array'
             THEN d.val->'data' ELSE '[]'::jsonb END) AS it
    WHERE d.key LIKE 'detail%'
),
clean AS (
    -- HTML 이스케이프 복원 + "값 없음"을 뜻하는 표기를 전부 NULL 로 정규화.
    --
    -- KOSHA MSDS 는 값이 없을 때 표기가 항목마다 다르다(실측 34종 기준):
    --   '자료없음'  — 인화점(I14) 등에서 사용
    --   '해당없음'  — IMDG 등급(N06) 4건, 용기등급(N08) 8건. 비위험물이라
    --                 해당 분류 자체가 없다는 뜻이므로 NULL 이 맞다.
    --   '-'         — 용기등급(N08) 6건. 가스류처럼 용기등급 개념이 없는 경우.
    -- 이걸 문자열로 두면 imdg_class='해당없음' 같은 값이 소비자(백엔드·LLM)에게
    -- 실제 등급처럼 보이고, 격리 판정 조인에서도 의미 없는 키가 된다.
    SELECT chem_id, item_code,
           nullif(btrim(replace(replace(replace(replace(
               item_detail, '&lt;', '<'), '&gt;', '>'), '&amp;', '&'), '&quot;', '"')),
               '') AS v0
    FROM item
),
clean2 AS (
    SELECT chem_id, item_code,
           CASE WHEN v0 IN ('자료없음', '자료 없음', '해당없음', '해당 없음',
                            '정보없음', '-', '–', 'N/A')
                THEN NULL ELSE v0 END AS v
    FROM clean
),
pick AS (
    SELECT chem_id,
           max(v) FILTER (WHERE item_code = 'I14')   AS flash_point_raw,
           max(v) FILTER (WHERE item_code = 'I32')   AS autoignition_raw,
           max(v) FILTER (WHERE item_code = 'I26')   AS vapor_density_raw,
           max(v) FILTER (WHERE item_code = 'N02')   AS un_no_msds,
           max(v) FILTER (WHERE item_code = 'N06')   AS imdg_class,
           max(v) FILTER (WHERE item_code = 'N08')   AS packing_group,
           max(v) FILTER (WHERE item_code = 'B02')   AS ghs_hazard,
           max(v) FILTER (WHERE item_code = 'B0402') AS ghs_pictogram,
           max(v) FILTER (WHERE item_code = 'B0404') AS signal_word,
           max(v) FILTER (WHERE item_code = 'B0406') AS h_statements
    FROM clean2 GROUP BY chem_id
)
SELECT
    m.chem_id,
    m.cas_no,
    m.name_ko,
    m.name_en,
    -- UN 번호: 로더 컬럼 우선, 비어 있으면 payload(N02)로 보강
    COALESCE(nullif(btrim(m.un_no), ''), p.un_no_msds)                  AS dg_un_no,
    p.flash_point_raw,
    -- 인화점 숫자화: '℃' 앞부분만 잘라(뒤쪽 압력값 오인 방지) 첫 실수를 취한다.
    --   '-37 ℃|※출처'           → -37
    --   '11.11 ℃|※출처'         → 11.11
    --   '< -40 ℃ (ca. 101.325)'  → -40   (℃ 앞만 보므로 101.325 무시)
    --   '58~66 ℃'                → 58    (범위는 하한 = 보수적, 낮을수록 위험)
    --   '<  ℃ (c.c.)' / '(인화성 가스)' → NULL (숫자 없음)
    (regexp_match(split_part(p.flash_point_raw, '℃', 1),
                  '(-?[0-9]+(?:\.[0-9]+)?)'))[1]::numeric              AS flash_point_celsius,
    p.imdg_class,
    p.packing_group,
    p.signal_word,
    p.h_statements,
    p.ghs_pictogram,
    p.ghs_hazard,
    p.autoignition_raw,
    p.vapor_density_raw
FROM msds_chemical m
LEFT JOIN pick p ON p.chem_id = m.chem_id;

-- ---------------------------------------------------------------------------
-- 4. mart.cargo_msds — 화물 ↔ MSDS 위험물 (★안전관제 핵심, 화물 행 단위)
--    UPA 화물 manifest 의 dg_un_no ↔ mart.msds_flat.dg_un_no 조인으로
--    화물 1건마다 인화점·IMDG 등급·GHS 정보를 붙인다. UN 번호가 없는 화물은
--    msds_matched=false 로 남는다 (일반 화물 or 코드 미기재 — 후자는 bzentyCd
--    확보로 통합화물 API 조회가 가능해지면 자동으로 채워진다).
--    ※ msds_chemical 을 직접 읽지 않고 mart.msds_flat 을 경유한다 — JSONB
--      평탄화 규칙을 한 곳에서만 관리하기 위함 (3-1 참고).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.cargo_msds AS
SELECT
    upper(trim(cm.callsgn))        AS callsgn,
    cm.bl_no,
    -- 접안 시설명 — 혼재금지(IMDG 격리) 판정에 필수.
    -- "인접 선석에서 비혼재 등급을 동시 취급 중인가"를 판정하려면 화물이 어느
    -- 부두에 있는지 알아야 한다 (Neo4j ADJACENT_TO 선석쌍과 대조).
    cm.facility_name,
    -- 포장·하역방식 — 용기등급 대비 적정성 판정용
    -- (예: 용기등급 Ⅰ 화물을 일반 드럼·크레인으로 신고한 경우)
    cm.cargo_se_name,
    cm.package_type_name,
    cm.unload_method_name,
    cm.cargo_name_raw,
    cm.dg_un_no,
    ms.chem_id,
    ms.cas_no,
    ms.name_ko                     AS msds_name_ko,
    ms.flash_point_celsius,
    ms.imdg_class,
    ms.packing_group,
    ms.signal_word,
    ms.ghs_hazard,
    ms.h_statements,
    (ms.chem_id IS NOT NULL)       AS msds_matched,
    -- 합성 데이터 여부를 반드시 노출한다. bzentyCd 미확보로 화물은 당분간
    -- 합성 샘플을 쓰는데, 뷰에서 이 플래그를 감추면 소비자(백엔드·대시보드·
    -- LLM)가 실데이터로 오인한다. 뷰에서 걸러내지 않는 이유는 시연에 합성
    -- 데이터가 필요하기 때문 — 걸러내는 대신 "표시"해서 판단을 넘긴다.
    cm.is_synthetic,
    cm.cargo_basis
FROM upa_cargo_manifest cm
-- ★ [2026-09-22] UN 번호 조인 -> chem_id 직결.
--
--   UN 번호는 화학물질 식별자가 아니라 **운송 분류 코드**다. 한 UN 에 여러
--   물질이 붙어 조인이 다대일로 팬아웃했다. 실측(2026-09-22):
--       매니페스트 757행 -> UN 조인 시 890행 (+133)
--       UN3082 -> 6종, UN1993 -> 4종(석유 포함), UN1986 -> 4종, UN3295 -> 4종
--
--   더 나쁜 건 그 뒤다. arrival_watcher._QUERY_PENDING_ARRIVALS 가
--       SELECT cm.chem_id FROM mart.cargo_msds cm
--       WHERE cm.callsgn = dc.callsgn AND cm.chem_id IS NOT NULL LIMIT 1
--   로 하나를 집는데 ORDER BY 가 없다. UN1993 화물의 안전판정이 '석유'로 갈지
--   '옥타메틸사이클로테트라실록산'으로 갈지가 우연이었다 — 둘은 인화점과 IMDG
--   등급이 달라 판정 결과가 실제로 바뀐다.
--
--   chem_id 는 매니페스트 생성 시점에 못 박는다(gen_cargo_manifest.UN_TO_CHEM_ID).
--   후보가 둘 이상이면 거기서 결정하고, 여기서는 1:1 로 붙이기만 한다.
--   실측 매칭률 79.7% (603/757).
LEFT JOIN mart.msds_flat ms
       ON ms.chem_id = nullif(btrim(cm.chem_id), '');

-- ---------------------------------------------------------------------------
-- 5. mart.weather_now — 환경 최신 (항상 정확히 1행)
--    기상·조위·파고 각 최신 관측 1건을 옆으로 붙인다 (observed_at_utc 기준).
--    앵커(1행) 기준 LEFT JOIN — 세 관측 테이블 중 일부가 비어 있어도(수집 전·
--    장애) 뷰가 0행이 되지 않고 해당 소스만 NULL 로 남는다 (CROSS JOIN 이었다면
--    한 테이블만 비어도 대시보드 환경 컬럼 전체가 사라진다).
--    울산 단일 관측 지점 전제 — 다지점 수집으로 바뀌면 station_id 필터 추가.
-- ---------------------------------------------------------------------------
--
-- [2026-08-15 개정] "가장 최근 행"이 아니라 "그 지표가 실제로 관측된 가장 최근 행"
--
--   항만기상(MMAF openWeatherNow · 울산항동방파제서단등대)은 관측값 칸이 빈 행을
--   시각만 채워 매시간 보낸다. 실측: 최근 8일 중 하루 24행 가운데 풍속이 들어있는
--   행은 0~6건뿐이었다. 최신 행 하나만 집으면 wind_speed_ms 가 NULL 이 되고,
--   소비하는 쪽은 그걸 "실데이터 없음"으로 보고 mock 기상으로 넘어간다 —
--   실관측이 있는데도 대시보드가 가짜 기상을 띄우게 된다.
--
--   풍속은 한 소스만 보지 않는다. 조위관측소(KHOA)와 부이(KMA)도 풍속을 함께
--   보내고, 실측상 이쪽이 훨씬 촘촘하다 (조위 1,803행 전부 / 부이 250행 전부 /
--   항만기상 248행 중 33건). 세 소스를 합쳐 그중 가장 최근 관측을 쓴다.
--   기상 판정(하역중단 등)은 풍속 신선도에 직접 걸리므로 이 차이가 곧 판정 가부다.
--
--   값이 오래됐다는 사실은 숨기지 않는다 — weather_observed_at_utc 가 그 풍속이
--   실제로 측정된 시각이라 소비하는 쪽이 신선도를 그대로 판단할 수 있다.
--   어느 관측소에서 온 값인지도 wind_source/wind_station_name 으로 드러낸다.
--
CREATE OR REPLACE VIEW mart.weather_now AS
WITH wind_all AS (
    -- 항만기상에는 돌풍 컬럼이 스키마상 없다 (결측이 아니라 미제공)
    SELECT observed_at_utc, wind_speed_ms, wind_dir_deg,
           NULL::double precision AS gust_ms,
           station_name, 'MMAF_PORT'::text AS wind_source
      FROM weather_obs WHERE wind_speed_ms IS NOT NULL
    UNION ALL
    SELECT observed_at_utc, wind_speed_ms, wind_dir_deg, gust_ms,
           station_name, 'KHOA_TIDE'
      FROM tide_obs WHERE wind_speed_ms IS NOT NULL
    UNION ALL
    -- 부이는 풍향·풍속 센서가 2조다 (1번이 주센서, 2번은 예비)
    SELECT observed_at_utc, wind_speed1_ms, wind_dir1_deg, gust1_ms,
           station_name, 'KMA_BUOY'
      FROM wave_obs WHERE wind_speed1_ms IS NOT NULL
),
w   AS (SELECT * FROM wind_all ORDER BY observed_at_utc DESC LIMIT 1),
wa  AS (SELECT * FROM weather_obs WHERE air_temp_c        IS NOT NULL ORDER BY observed_at_utc DESC LIMIT 1),
wvi AS (SELECT * FROM weather_obs WHERE visibility_m      IS NOT NULL ORDER BY observed_at_utc DESC LIMIT 1),
t   AS (SELECT * FROM tide_obs    WHERE tide_level_cm     IS NOT NULL ORDER BY observed_at_utc DESC LIMIT 1),
v   AS (SELECT * FROM wave_obs    WHERE wave_height_sig_m IS NOT NULL ORDER BY observed_at_utc DESC LIMIT 1)
SELECT
    w.observed_at_utc      AS weather_observed_at_utc,
    w.wind_dir_deg,
    w.wind_speed_ms,
    wa.air_temp_c,
    wa.humidity_pct,
    wa.air_pressure_hpa,
    wvi.visibility_m,
    t.observed_at_utc      AS tide_observed_at_utc,
    t.tide_level_cm,
    t.sea_temp_c,
    t.salinity_psu,
    v.observed_at_utc      AS wave_observed_at_utc,
    v.wave_height_sig_m,
    v.wave_height_max_m,
    v.wave_period_s,
    v.wave_dir_deg,
    -- ↓ 실무 반영 추가 (CREATE OR REPLACE 제약상 맨 뒤에 붙인다)
    --   돌풍(gust): 계류삭 장력은 평균풍속이 아니라 순간최대풍속에 끊어진다.
    --     풍속과 같은 관측에서 온 값이어야 짝이 맞으므로 풍속 소스에서 함께 가져온다.
    --   조류(current): 액체부두 접·이안에서 조류는 풍속만큼 중요한 제약이다.
    --     특히 울산 본항·온산 수로는 창·낙조류 방향이 접안 조종에 직접 영향.
    w.gust_ms,
    t.current_speed_cms,
    t.current_dir_deg,
    -- 풍속 출처 (맨 뒤 추가) — 관제사·리뷰어가 "어느 관측소 값인가"를 알 수 있어야 한다
    w.wind_source,
    w.station_name         AS wind_station_name
FROM (SELECT 1) AS anchor
LEFT JOIN w   ON TRUE
LEFT JOIN wa  ON TRUE
LEFT JOIN wvi ON TRUE
LEFT JOIN t   ON TRUE
LEFT JOIN v   ON TRUE;

-- ---------------------------------------------------------------------------
-- 5-1. mart.berth_draught_check — 조위 반영 가용수심 · UKC 판정
--
-- [왜 필요한가 — 실무와 어긋나던 부분]
--   기존 흘수 판정은 "선박 흘수 < 부두 수심" 이었다. 항만 실무는 그렇지 않다.
--
--   (1) 부두 명세의 수심은 **해도기준면(Chart Datum) 기준**이다. 해도기준면은
--       약최저저조위이므로, 실제 그 시각의 가용수심은 항상 그보다 크다.
--           가용수심 = 해도수심 + 그 시각 조위
--       조위를 빼먹으면 "만조 때만 접안 가능한 배"를 영구 접안불가로 오판한다.
--       실제로 대형 유조선은 이 조위 여유에 맞춰 접안 시각을 잡는다.
--
--   (2) 흘수와 가용수심이 같으면 되는 게 아니다. 선체 아래에 **UKC(Under Keel
--       Clearance, 용골하 여유수심)** 가 남아야 한다. 항주파·선체 침하(squat)·
--       해저 기복·측심 오차를 흡수하는 여유다.
--
--   ※ UKC 요구치는 법정 수치가 아니다. 여기서는 통상 관행값(흘수의 10%)을
--     기본으로 두되, 터미널·항만별로 다르므로 파라미터로 분리해 두었다.
--     울산항 각 터미널 운영규정을 입수해 확정해야 한다(미확보 — 한계로 보고).
--
--   ※ AIS drft(최대정적흘수)는 선원이 입력하는 값이며 트림(선수·선미 흘수 차)을
--     반영하지 않는다. 실제 최대흘수는 이보다 클 수 있다 — 보수적으로 볼 것.
--
-- 판정:
--   OK           UKC 여유 충족
--   MARGINAL     양수이나 요구 UKC 미달 — 조위창(tidal window) 조정 필요
--   NOT_ALLOWED  가용수심 ≤ 흘수 — 착저 위험, 접안 불가
--   UNKNOWN      흘수 또는 부두 수심 미상
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.berth_draught_check AS
WITH tide AS (
    SELECT tide_level_cm, observed_at_utc
    FROM tide_obs ORDER BY observed_at_utc DESC LIMIT 1
),
wharf_depth AS (
    -- [2026-09-22] 수심 하드코딩(VALUES 35부두) -> wharf 테이블(65부두).
    --
    -- 왜 바꾸나: 같은 값이 SQL 과 seed CSV 두 곳에 있어 정본이 갈렸고, 목록에
    -- 없는 부두는 전부 UNKNOWN 이었다. wharf 는 울산항시설현황 웹 + API 대조로
    -- 만든 정본이다(alembic 0021, data/seed/berth_seed.csv).
    --
    -- 대조 검증(2026-09-22): 하드코딩돼 있던 13개 부두의 최소수심이 wharf 값과
    -- 전부 일치했다. 게다가 wharf 는 최대수심도 갖고 있어 berth_range 하드코딩
    -- (SK5 11 · SK2 8)까지 같이 걷어낸다 — 실제로 wharf 는 그 둘에 더해
    -- 용연·신항컨·2부두·4부두도 선석별 수심차가 있음을 알려준다.
    --
    -- 선석 번호는 여전히 모른다. vessel_presence 는 부두까지만 알려주므로
    -- chart_depth_m 은 늘 부두 최소수심이다 — 안전측 최소값 원칙 그대로다.
    --
    -- 부이는 뺀다. 해상 계류라 안벽 UKC 개념이 다르다(수심 27m 로 들어와 있어
    -- 그대로 두면 무조건 OK 가 된다).
    SELECT w.wharf_name,
           w.min_water_depth_m::double precision AS chart_depth_m,
           w.max_water_depth_m::double precision AS depth_max_m
    FROM wharf w
    WHERE w.min_water_depth_m IS NOT NULL
      AND strpos(w.wharf_name, '부이') = 0
),
ais_draught AS (
    -- 흘수 0 은 "0m"가 아니라 "선박이 보내지 않음"이다(dev 2026-09-21).
    SELECT DISTINCT ON (upper(btrim(callsgn)))
           upper(btrim(callsgn)) AS callsgn, draught, received_at_utc
    FROM upa_vessel_position
    WHERE nullif(btrim(callsgn), '') IS NOT NULL AND draught > 0
    ORDER BY upper(btrim(callsgn)), received_at_utc DESC
),
vessel AS (
    -- [2026-09-22] AIS 미송출 시 PORT-MIS 등록 흘수로 폴백 — mart.dashboard_current
    --   의 draught 와 같은 규칙이어야 한다. 두 화면이 서로 다른 흘수를 쓰면
    --   UKC 표와 판정 결과가 어긋난다(같은 배에 다른 답).
    --   received_at_utc 는 AIS 관측에만 있는 값이라 폴백 행에서는 NULL —
    --   화면의 '흘수 관측시각'이 등록값을 관측인 양 보이면 안 되기 때문이다.
    SELECT COALESCE(a.callsgn, upper(btrim(vs.callsgn)))    AS callsgn,
           COALESCE(a.draught, vs.draught_m)                AS draught,
           a.received_at_utc
    FROM ais_draught a
    FULL JOIN vessel_spec vs
           ON upper(btrim(vs.callsgn)) = a.callsgn
    WHERE COALESCE(a.draught, vs.draught_m) IS NOT NULL
),
pc AS (
    SELECT callsgn, berth_name AS facility_name, vts_arrival_at_utc AS arrival_at_utc
    FROM mart.vessel_presence
    WHERE presence_zone = 'BERTH' AND callsgn IS NOT NULL
)
SELECT
    pc.callsgn,
    pc.facility_name,
    b.chart_depth_m,
    (SELECT tide_level_cm FROM tide) / 100.0            AS tide_level_m,
    b.chart_depth_m + COALESCE((SELECT tide_level_cm FROM tide), 0) / 100.0
                                                        AS available_depth_m,
    v.draught                                           AS vessel_draught_m,
    round((b.chart_depth_m + COALESCE((SELECT tide_level_cm FROM tide), 0) / 100.0
           - v.draught)::numeric, 2)                    AS ukc_m,
    round((v.draught * 0.10)::numeric, 2)               AS ukc_required_m,
    CASE
        WHEN v.draught IS NULL OR b.chart_depth_m IS NULL THEN 'UNKNOWN'
        WHEN b.depth_max_m IS NOT NULL
             AND b.depth_max_m > b.chart_depth_m
             AND (b.chart_depth_m + COALESCE((SELECT tide_level_cm FROM tide), 0) / 100.0
                  - v.draught) < v.draught * 0.10
             AND (b.depth_max_m + COALESCE((SELECT tide_level_cm FROM tide), 0) / 100.0
                  - v.draught) >= v.draught * 0.10       THEN 'CHECK'
        WHEN (b.chart_depth_m + COALESCE((SELECT tide_level_cm FROM tide), 0) / 100.0)
             <= v.draught                                THEN 'NOT_ALLOWED'
        WHEN (b.chart_depth_m + COALESCE((SELECT tide_level_cm FROM tide), 0) / 100.0
              - v.draught) < v.draught * 0.10            THEN 'MARGINAL'
        ELSE 'OK'
    END                                                 AS draught_verdict,
    (SELECT observed_at_utc FROM tide)                  AS tide_observed_at_utc,
    v.received_at_utc                                   AS draught_observed_at_utc,
    pc.arrival_at_utc,
    COALESCE(b.depth_max_m, b.chart_depth_m)            AS chart_depth_max_m
FROM pc
-- ★ LEFT JOIN 이어야 한다(dev 2026-08-15 의 근거를 그대로 둔다). INNER JOIN 이면
--   wharf 에 제원이 없는 부두(부이·신규 부두)에 접안한 선박이 판정 결과에서 통째로
--   사라진다. 그러면 "위험하지 않다"가 아니라 "아예 안 보인다"가 되어
--   UNIDENTIFIED·NO_SIGNAL 을 살려둔 이 프로젝트 원칙과 정면으로 어긋난다.
--   제원이 없으면 chart_depth_m 이 NULL 이 되고 draught_verdict 는 'UNKNOWN' 이다.
LEFT JOIN mart.facility_alias fa
       ON fa.source_name = pc.facility_name AND fa.facility_type = 'BERTH'
-- ★ 마스터 표기로 먼저 직접 붙이고, 안 되면 facility_alias 를 거친다.
--   vessel_presence 는 이미 마스터 표기(berth_name)를 준다. 그런데
--   facility_alias 에 그 이름이 source_name 으로 늘 있지는 않다 — 실측
--   (2026-09-22): 접안 8개 부두 중 '4부두'·'신항북방파제 T/S부두' 두 곳은
--   source_name 에 '4부두 01'·'4부두 02' 같은 선석 표기만 있고 부두명 자체가
--   없어 조인이 끊겼다. 그 결과 wharf 에 수심이 있는데도 UNKNOWN 이 됐다.
--   COALESCE 순서를 '직접 -> 별칭'으로 두면 둘 다 붙는다.
LEFT JOIN wharf_depth b ON b.wharf_name = COALESCE(
    (SELECT w2.wharf_name FROM wharf_depth w2 WHERE w2.wharf_name = pc.facility_name),
    fa.wharf_name
)
LEFT JOIN vessel v ON v.callsgn = pc.callsgn;

-- ---------------------------------------------------------------------------
-- 6. mart.dashboard_current — '한 줄 조회' (대시보드·에이전트 진입점)
--    선박별 최신 위치 1행에 식별·입항·위험물 요약·환경 최신을 전부 결합.
--    거리·ETA 는 create_mart.py 와 동일 공식(하버사인, 지구반경 3440.065 해리,
--    목표 좌표 35.475/129.387)으로 산출 — FastAPI/에이전트는 이 뷰만 조회한다.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.dashboard_current AS
WITH msds_by_vessel AS (
    SELECT callsgn,
           min(flash_point_celsius)                    AS min_flash_point_c,
           string_agg(DISTINCT imdg_class, ',')        AS imdg_classes,
           string_agg(DISTINCT signal_word, ',')       AS ghs_signal_words,
           count(*) FILTER (WHERE msds_matched)        AS msds_matched_count
    FROM mart.cargo_msds
    GROUP BY callsgn
)
SELECT
    -- [선박 식별]
    -- vessel_identity 에 아직 없는 선박(레거시 AIS 관측만 있는 경우)도 최소한
    -- 위치 소스가 들고 있는 MMSI/키는 살려 둔다 — NULL 행이 생기지 않게.
    COALESCE(vi.vessel_key, p.vessel_pos_key)           AS vessel_key,
    p.callsgn,
    vi.imo_no,
    COALESCE(vi.mmsi, p.mmsi)                           AS mmsi,
    vi.vessel_name,
    vi.ship_kind_nm,
    vi.is_liquid_cargo_vessel,
    vi.nationality_nm,
    -- [최신 위치·상태]
    p.latitude,
    p.longitude,
    p.sog,
    p.cog,
    p.heading,
    -- [2026-09-22] AIS 흘수 0 → PORT-MIS 등록 흘수(vessel_spec) 폴백.
    --   AIS 규격상 draught=0 은 "0m"가 아니라 "미송출"이다. 그런데 이 값이
    --   그대로 arrival_watcher 로 흘러가 "흘수 정보가 없어 수심 여유를 계산할
    --   수 없습니다"(판정불가)가 됐다 — 실측 2건(다인3호·비케이25). 두 배 모두
    --   vessel_spec 에 흘수가 멀쩡히 있었다(3.4m·4.4m). 근거가 없어서가 아니라
    --   있는 근거를 안 읽어서 난 판정불가라, 회의 §4 의 '근거 부족'에 해당하지
    --   않는다.
    --   방향도 안전측이다 — vessel_spec 은 만재흘수라 실측보다 크거나 같다
    --   (둘 다 있는 184척 중 136척에서 spec >= AIS, 평균 7.42m vs 6.34m).
    --   AIS 값이 있으면 그쪽이 언제나 우선이다(실제 적재 상태를 반영하므로).
    COALESCE(nullif(p.draught, 0), vs.draught_m)        AS draught,
    p.nav_status_code,
    p.received_at_utc,
    p.position_source,
    p.quality_flag,
    -- [거리·ETA — 울산항 목표 좌표 기준]
    d.distance_to_ulsan_nm,
    CASE WHEN p.sog > 0.5 THEN d.distance_to_ulsan_nm / p.sog END AS eta_hours,
    -- [입항·접안]
    pco.arrival_at_utc,
    pco.departure_at_utc,
    pco.facility_name,
    pco.entry_purpose_nm,
    pco.dest_port_nm,
    -- [화물·위험물]
    pco.cargo_item_count,
    pco.dg_cargo_count,
    pco.dg_un_nos,
    mv.min_flash_point_c,
    mv.imdg_classes,
    mv.ghs_signal_words,
    mv.msds_matched_count,
    -- [환경 최신 — 모든 행에 동일 부착]
    wn.wind_speed_ms,
    wn.visibility_m,
    wn.tide_level_cm,
    wn.wave_height_sig_m,
    wn.weather_observed_at_utc,
    -- ↓ 신규 컬럼 (CREATE OR REPLACE 제약상 반드시 맨 뒤에 추가할 것)
    -- 식별 신뢰도 — UNIDENTIFIED 는 '안전'이 아니라 '미확인 위험'으로 표시할 것.
    vi.identity_confidence,
    vi.callsgn_source,
    -- 풍향·돌풍: 이안풍(offshore)이면 계류삭 장력이 급증하고, 계류 판단은
    -- 평균풍속이 아니라 순간최대풍속(gust)으로 한다.
    wn.wind_dir_deg,
    wn.gust_ms,
    -- 조류: 액체부두 접·이안 조종의 직접 제약.
    wn.current_speed_cms,
    wn.current_dir_deg,
    -- -----------------------------------------------------------------------
    -- 존재 상태 — 프론트 진입점이 여기이므로 반드시 노출해야 한다.
    --   프론트 기본 필터: presence_state = 'PRESENT'
    --   'STALE'     회색 반투명 + position_age_min 배지
    --   'NO_SIGNAL' ★숨기지 말 것. doc_still_in_port=true 이면
    --               "서류상 재항인데 신호 소실" — 관제사 확인 대상.
    --   'DEPARTED'  출항신고 확정 — 지도에서 제외해도 안전
    -- -----------------------------------------------------------------------
    p.position_age_min,
    p.presence_state,
    p.doc_still_in_port,
    p.signal_health,
    -- 위 draught 가 어디서 왔는지. 판정 근거를 화면·보고서가 숨기지 않게 한다.
    --   AIS      실제 적재 상태(관측값)
    --   REGISTER PORT-MIS 등록 만재흘수(보수적 대체값)
    --   NULL     양쪽 다 없음 → 판정불가가 맞는 답
    CASE WHEN nullif(p.draught, 0) IS NOT NULL THEN 'AIS'
         WHEN vs.draught_m IS NOT NULL         THEN 'REGISTER'
    END                                                 AS draught_source
FROM mart.vessel_latest_position p
CROSS JOIN LATERAL (
    SELECT CASE
        WHEN p.latitude IS NOT NULL AND p.longitude IS NOT NULL THEN
            3440.065 * 2 * asin(sqrt(
                power(sin(radians((35.475 - p.latitude) / 2)), 2)
                + cos(radians(p.latitude)) * cos(radians(35.475))
                  * power(sin(radians((129.387 - p.longitude) / 2)), 2)
            ))
    END AS distance_to_ulsan_nm
) d
-- [MMSI-First] 식별 조인을 callsgn 이 아니라 vessel_uid(=MMSI 기반)로 건다.
-- callsgn 을 한 번도 송출하지 않는 관공선·소방정·예부선도 선명·MMSI 를 달고
-- 대시보드에 표시되어야 하기 때문이다. 반대로 입항신고·화물은 callsgn 이
-- 있어야만 존재하는 정보라 그대로 callsgn 조인을 유지한다.
LEFT JOIN mart.vessel_identity     vi  ON vi.vessel_uid = p.vessel_pos_key
LEFT JOIN mart.port_call_overview  pco ON pco.callsgn = p.callsgn
LEFT JOIN msds_by_vessel           mv  ON mv.callsgn = p.callsgn
-- 흘수 폴백 전용 — callsgn 당 1행임을 확인했다(410행 / 고유 410).
LEFT JOIN vessel_spec              vs  ON upper(btrim(vs.callsgn)) = p.callsgn
LEFT JOIN mart.weather_now         wn  ON TRUE;

-- ---------------------------------------------------------------------------
-- 7. mart.pipeline_health — 수집기 생존 신호 (관측성)
--
-- ★ 왜 필요한가
--   수집기가 7시간 죽으면 모든 선박이 NO_SIGNAL 로 떨어지고 지도가 텅 빈다.
--   화면상으로는 "울산항에 배가 한 척도 없음"과 구별되지 않는다.
--   프론트는 pipeline_state='DEGRADED' 일 때 지도를 비우는 대신
--   "수집 지연 — 표시 정보가 최신이 아닙니다" 배너를 띄워야 한다.
--
--   collected_at_utc(우리 수집 시각)와 received_at_utc(UPA 갱신 시각)를
--   분리해 둔 덕분에 추가 수집 없이 계산된다.
--     - collect_age_min 이 크다        → 우리 파이프라인 장애
--     - source_age_min 만 크다         → UPA 쪽 갱신 지연 (우리는 정상)
--   이 둘을 구분해야 장애 대응 대상이 정해진다.
--
-- ★ 2026-08-04 수정 — 초록불 오탐 제거
--   이전 판은 pipeline_state 를 collected_at_utc 하나로만 판정했다. 그 결과
--   "옛날 raw 파일을 다시 전처리하기만 해도" OK 가 떴다. 실측 재현:
--       collect_age_min 1분(방금 돌림) / source_age_min 31,011분(21.5일 전 데이터)
--       → pipeline_state = 'OK'          ← 3주 전 위치를 정상이라고 표시
--   관제사가 초록 배지를 보고 안심하는데 화면에는 3주 전 선박이 떠 있는
--   상황이 가능하다. 이 뷰가 막으려던 사고와 정확히 같은 종류다.
--
--   그래서 상태를 2개(OK/DEGRADED)에서 4개로 나누고, 원인별로 대응 주체가
--   갈리도록 diagnosis 문장을 함께 낸다. 판정 순서가 중요하다 —
--   수집기가 죽었으면 원천 신선도는 볼 필요도 없으므로 COLLECTOR_DOWN 이 먼저다.
--
--     OK              둘 다 최신
--     COLLECTOR_DOWN  우리 수집기가 120분 이상 안 돎       → 데이터 담당
--     STALE_SOURCE    수집은 도는데 원천이 180분 이상 낡음 → UPA 문의 / 옛 raw 재처리 의심
--     NO_DATA         적재 데이터 자체가 없음              → 초기 상태
--
-- ★ 2026-08-10 임계값 재산정 (20분/60분 → 120분/180분)
--   이전 값(20분/60분)은 로컬에서 6분 주기로 폴링하던 시절 기준이었다.
--   AWS 무인 수집기(PR #22)로 넘어가면서 수집 주기가 **매시 정각 1회**가 됐고,
--   그 결과 정상 운영 중에도 장애로 표시되는 반대 방향 오탐이 생겼다.
--     실측(PR #23 리뷰): 마지막 수집 27분 경과 — 정상인데 COLLECTOR_DOWN
--
--   이 뷰는 원래 "초록불 오탐"(장애인데 정상이라 표시)을 막으려고 만든 것인데,
--   반대로 "빨간불 오탐"(정상인데 장애라 표시)이 잦으면 관제사가 배지를
--   무시하게 되어 결국 같은 곳으로 간다. 두 방향 다 막아야 의미가 있다.
--
--   새 값의 근거 — 수집 주기 60분 기준:
--     COLLECTOR_DOWN 120분 = 정각 수집을 2회 연속 놓침. 1회 실패는 일시적
--       네트워크 오류로도 발생하므로 즉시 장애로 부르지 않는다.
--     STALE_SOURCE  180분 = 원천 신선도는 "수집 주기(최대 60분 지연) + 원천
--       자체 갱신 지연"의 합이라 수집기 기준보다 넉넉해야 한다. 60분으로 두면
--       매시 수집 직전(59분 경과)마다 STALE_SOURCE 가 깜빡인다.
--
--   ※ 수집 주기가 다시 바뀌면 이 두 값도 같이 바꿔야 한다. 근거는
--     mart_views_check.sql 의 관측간격 분위수 쿼리로 다시 만들 수 있다.
--
--   ※ 프론트 규약: pipeline_state <> 'OK' 이면 지도를 비우는 대신 배너를
--     띄운다. COLLECTOR_DOWN 과 STALE_SOURCE 는 문구가 달라야 한다 —
--     전자는 "우리 시스템 점검 중", 후자는 "원천 데이터 갱신 지연"이다.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.pipeline_health AS
SELECT
    max(collected_at_utc)                                                  AS last_collect_at_utc,
    round(EXTRACT(EPOCH FROM (now() - max(collected_at_utc))) / 60.0)::int AS collect_age_min,
    max(received_at_utc)                                                   AS last_source_at_utc,
    round(EXTRACT(EPOCH FROM (now() - max(received_at_utc))) / 60.0)::int  AS source_age_min,
    count(*) FILTER (WHERE collected_at_utc > now() - interval '10 min')   AS rows_last_10min,
    count(DISTINCT vessel_uid)                                             AS vessel_uid_total,
    CASE
        WHEN max(collected_at_utc) IS NULL                        THEN 'NO_DATA'
        WHEN max(collected_at_utc) < now() - interval '120 min'   THEN 'COLLECTOR_DOWN'
        WHEN max(received_at_utc)  < now() - interval '180 min'   THEN 'STALE_SOURCE'
        ELSE 'OK'
    END                                                                    AS pipeline_state,
    -- ↓ 신규 컬럼 (CREATE OR REPLACE 제약상 반드시 맨 뒤에 추가할 것)
    -- 관제사·개발자가 "그래서 뭘 해야 하나"를 배지 하나로 알 수 있게 한다.
    CASE
        WHEN max(collected_at_utc) IS NULL
            THEN '적재 데이터 없음 — 파이프라인을 한 번도 돌리지 않았거나 테이블이 비어 있음'
        WHEN max(collected_at_utc) < now() - interval '120 min'
            THEN '우리 수집기가 안 돌고 있음(정각 수집 2회 이상 누락) — EC2 스케줄러/run_pipeline 상태 확인'
        WHEN max(received_at_utc) < now() - interval '180 min'
            THEN '수집은 도는데 원천 데이터가 낡음 — UPA 갱신 지연 또는 옛 raw 재처리 의심'
        ELSE '정상'
    END                                                                    AS diagnosis
FROM upa_vessel_position;

-- ---------------------------------------------------------------------------
-- 8. mart.berth_current_cargo — 선석별 "지금 취급 중인 화물" (백엔드 소비 계약)
--
-- ★ 왜 필요한가 — 백엔드의 알려진 한계 #1 을 없애는 뷰다.
--   backend/app/agents/scheduling/category_map.py 는 인접 선석의 화물을 몰라서
--   카테고리 대표 1종으로 근사하고 있다:
--       원유 → 석유(000751) / 유류 → 디젤(000973) / 액체화학 → 벤젠(001008)
--   그 파일 주석도 "upa_cargo_manifest 파이프라인이 생기면 이 모듈 대신 실제
--   최근 하역 기록을 조회하도록 교체해야 한다"고 적어두었다. 이 뷰가 그 대체물이다.
--
--   실제 영향(그 문서 인용): "정일1부두에 실제로는 톨루엔이 있는데도 시스템은
--   '액체화학이니까 벤젠이 있다고 치자'고 판단한다" — 즉 혼재금지 판정이 엉뚱한
--   화물 조합으로 이뤄진다. 이 뷰를 쓰면 실제 신고 화물로 판정하게 된다.
--
-- ★ 출력 계약 — 스케줄링 에이전트가 그대로 쓸 수 있는 모양
--   AdjacentCargo(berth_name=facility_name, cargo=CargoRef(chem_id=chem_id))
--   CargoRef 는 chem_id 또는 cas_no 중 하나면 되므로 둘 다 내보낸다.
--   (백엔드는 un_no 로 조회하지 않는다 — msds_context.py 는 cas_no/chem_id 만 쓴다.
--    un_no 는 msds_chemical 의 표시용 컬럼이고 WHERE 절에 등장하지 않는다.
--    화물 manifest 에는 UN 번호만 있으므로, UN → chem_id 변환이 이 뷰의 핵심이다.)
--
-- ★ "지금"의 정의 (2026-09-17 개정)
--   mart.vessel_presence(2-1)에서 presence_zone='BERTH' 인 배 = 지금 선석에 있는 배.
--   예전엔 "upa_port_call 에 입항 기록이 있고 departure_at_utc 가 NULL"이었는데,
--   출항 처리가 빠진 유령 기록(7일 넘은 건이 대부분)과 정박지 대기 배까지 들어가
--   옆 선석 혼재 판정이 있지도 않은 배의 화물로 이뤄졌다.
--   facility_name 은 그 배가 실제로 붙어 있는 선석(마스터 표기)이다. 합성 manifest
--   의 facility_name 은 생성 당시 가정한 선석이라 위치 판정 선석을 우선한다.
--
-- ★ 한계 (숨기지 않는다)
--   - is_synthetic=true 인 화물이 섞여 있다. bzentyCd(업체코드) 미확보로 UPA
--     통합화물 API 를 못 부르는 동안의 합성 샘플이다. 소비자가 판단하도록 그대로
--     노출한다. 실화물만 원하면 WHERE is_synthetic = false 를 붙일 것.
--   - MSDS 에 없는 UN 번호는 chem_id 가 NULL 이다. 이 행을 버리지 않는다 —
--     "위험물이 있는데 정체를 모른다"는 것이 "화물이 없다"보다 중요한 정보다.
--     소비자는 chem_id IS NULL 을 '미확인 위험'으로 다뤄야 한다.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.berth_current_cargo AS
WITH in_port AS (
    -- 지금 선석에 있는 배 (UPA 위치 판정, 선박당 1행)
    SELECT callsgn,
           berth_name          AS facility_name,
           vts_arrival_at_utc  AS arrival_at_utc
    FROM mart.vessel_presence
    WHERE presence_zone = 'BERTH' AND callsgn IS NOT NULL
)
SELECT DISTINCT
       ip.facility_name,
       ip.callsgn,
       cm.chem_id,
       cm.cas_no,
       cm.dg_un_no,
       COALESCE(cm.msds_name_ko, cm.cargo_name_raw)  AS cargo_name,
       cm.imdg_class,
       cm.packing_group,
       cm.msds_matched,
       cm.is_synthetic,
       cm.cargo_basis,
       ip.arrival_at_utc
FROM in_port ip
JOIN mart.cargo_msds cm ON cm.callsgn = ip.callsgn
WHERE cm.dg_un_no IS NOT NULL;   -- 위험물 화물만 (혼재금지 판정 대상)


-- ---------------------------------------------------------------------------
-- 11. mart.berth_dwell_stats   선석별 재항 소요시간 실측 통계
--     (upa_port_call 완료 건: 입항 ~ 출항 실측 29,607건 기준)
--
-- 왜 필요한가: 스케줄링 에이전트가 지금까지 답할 수 있는 것은 "이 선석이
-- 점유인가 여유인가" 둘뿐이었다. 그런데 관제사가 실제로 묻는 것은 "그럼
-- 언제 비는가"다. 그 답이 없으면 '배정'은 되지만 '스케줄링'은 되지 않는다.
--
-- 출항 예정 시각(ETD)이 있으면 그걸 쓰는 게 맞지만, portmis_vessel.
-- departure_sched_utc 는 779행 전부 비어 있다(2026-08-18 실측 — 원천 API 가
-- 이 필드를 주지 않는다). 대신 우리에게는 실제로 몇 시간 머물렀는지가
-- 3만 건 가까이 쌓여 있으므로, 그 분포에서 추정한다.
--
-- 평균이 아니라 중앙값(P50)을 대표값으로 쓴다. 재항시간은 꼬리가 매우 길어
-- (장생포호안 평균 127h vs 중앙값 39h) 평균은 몇 건의 장기 계류에 끌려간다.
-- P90 을 함께 내보내 "보통 이 정도, 길면 이 정도"를 화면이 같이 말할 수 있게 한다.
--
-- 주의 — 이 값은 '하역 시간'이 아니라 '재항 시간'이다. 접안 대기·검사·급유가
-- 모두 포함돼 있어 실제 하역 작업시간보다 길다. 화면은 이 값을 '하역 소요'가
-- 아니라 '재항 소요(해제까지)'로 표기해야 한다.
--
-- 표본이 5건 미만인 선석은 내보내지 않는다 — 한두 건으로 만든 중앙값을
-- 화면이 예측처럼 보여주면 근거 없는 숫자가 된다(모르면 말하지 않는다).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.berth_dwell_stats AS
SELECT fa.wharf_name,
       count(*)                                                   AS sample_count,
       round((percentile_cont(0.5) WITHIN GROUP (
           ORDER BY EXTRACT(EPOCH FROM (pc.departure_at_utc - pc.arrival_at_utc)) / 3600.0
       ))::numeric, 1)                                            AS median_hours,
       round((percentile_cont(0.9) WITHIN GROUP (
           ORDER BY EXTRACT(EPOCH FROM (pc.departure_at_utc - pc.arrival_at_utc)) / 3600.0
       ))::numeric, 1)                                            AS p90_hours,
       max(pc.departure_at_utc)                                   AS latest_sample_utc
FROM upa_port_call pc
JOIN mart.facility_alias fa
  ON fa.source_name = pc.facility_name AND fa.facility_type = 'BERTH'
WHERE pc.arrival_at_utc IS NOT NULL
  AND pc.departure_at_utc IS NOT NULL
  AND pc.departure_at_utc > pc.arrival_at_utc
  -- 30일을 넘는 건은 계선(장기 정박)으로 보고 뺀다. 하역 회전과 성격이 달라
  -- 같이 섞으면 중앙값이 위로 끌려간다.
  AND pc.departure_at_utc - pc.arrival_at_utc < INTERVAL '30 days'
GROUP BY fa.wharf_name
HAVING count(*) >= 5;


-- ---------------------------------------------------------------------------
-- 12. mart.berth_handling_cargo   선석 취급화물 (큐레이션 보정 반영, 단일 정본)
--
-- 원천 upa_berth_facility.handling_cargo_name 은 대분류라, 물리적으로 전혀 다른
-- 설비를 같은 '유류'로 묶어 놓은 곳이 있다. 그 보정을 지금까지 파이썬 쪽
-- (berth_neo4j_loader.HANDLING_CARGO_OVERRIDES)에만 두었더니, Neo4j 를 읽는
-- 스케줄링 에이전트는 '가스'로 판정하는데 SQL 을 읽는 화면(선석 배정현황)은
-- '유류'로 표시하는 어긋남이 생겼다(2026-08-20 실측).
--
-- 같은 판정 기준이 두 군데 살아 있으면 반드시 갈라진다. 그래서 보정을 이 뷰
-- 하나에만 두고, Neo4j 로더와 백엔드 API 가 똑같이 여기서 읽는다.
--
-- 보정 근거:
--  · 석유공사부이 — 수심 27 m 해상 계류점(SPM), 운영사 한국석유공사(원유비축기지).
--    같은 데이터의 다른 부이 4기는 전부 '원유'다. '유류'로 두면 흘수 6 m 제품유
--    운반선에게 VLCC 용 부이가 후보로 올라온다.
--  · 가스부두 / SK1부두 / SK2부두(SK가스㈜ 운영분) — LPG 전용 터미널이다. 가압·
--    냉동 탱크와 증기환수 배관이 필요해 일반 석유제품 부두와 같이 묶을 수 없다.
--    ※ 'SK2부두' 는 SK가스㈜(수심 7.5)와 SK에너지㈜(수심 8.0) 두 곳이 이름을
--      공유한다 — 그래서 보정 키가 (선석명, 운영사) 다.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.berth_handling_cargo AS
SELECT bf.record_uid,
       bf.wharf_name,
       bf.port_name,
       bf.port_operator_name,
       bf.handling_cargo_name                       AS source_handling_cargo_name,
       CASE
           WHEN bf.wharf_name = '석유공사부이' AND bf.port_operator_name = '한국석유공사'
               THEN '원유'
           WHEN bf.wharf_name IN ('가스부두', 'SK1부두', 'SK2부두')
                AND bf.port_operator_name = 'SK가스㈜'
               THEN '가스'
           ELSE bf.handling_cargo_name
       END                                          AS handling_cargo_name,
       (
           CASE
               WHEN bf.wharf_name = '석유공사부이' AND bf.port_operator_name = '한국석유공사'
                   THEN '원유'
               WHEN bf.wharf_name IN ('가스부두', 'SK1부두', 'SK2부두')
                    AND bf.port_operator_name = 'SK가스㈜'
                   THEN '가스'
               ELSE bf.handling_cargo_name
           END
       ) IS DISTINCT FROM bf.handling_cargo_name    AS is_corrected
FROM upa_berth_facility bf;


-- ===========================================================================
-- 7. 감사·승인 뷰 (2026-09-22 통합)
--
-- 아래 뷰들은 별도 파일(berth_audit_views.sql · anchorage_audit_views.sql ·
-- approval_candidates.sql · arrival_views_v2.sql)로 따로 관리하다가 여기로
-- 합쳤다. 따로 두면 이 파일이 DROP 하는 원본(facility_alias ·
-- vessel_latest_position · cargo_msds)에 매달린 채 남아, 다음 적용 때
-- "other objects depend on it" 으로 막힌다 — 실제로 막혔다(2026-09-22).
-- 삭제는 위 DROP 구역 맨 앞에 함께 있다.
--
-- ★ mart.berth_occupancy_live 는 여기 없다.
--   같은 질문("지금 어느 배가 어느 선석에 붙어 있나")에 2-1 의 mart.vessel_presence
--   와 둘이 서로 다른 답을 냈다. 실측(2026-09-22, 같은 스냅샷):
--       vessel_presence      접안 14척 (위치 10 · 신고+위치 4)
--       berth_occupancy_live 접안  0척
--   berth_occupancy_live 는 now() 기준 30분 안의 위치만 '접안'으로 봤는데, 그때
--   최신 위치가 6시간 33분 전이라 전부 NO_SIGNAL 로 떨어졌다. 수집이 잠깐만
--   밀려도 화면이 통째로 빈다. vessel_presence 는 최신 스냅샷 기준으로 판정하고
--   낡은 정도를 quality_flag·position_age_min 으로 따로 밝힌다 — 그쪽이 맞다.
-- ===========================================================================

-- --- 7-1. mart.arrival_schedule (PORT-MIS 입항 예정) ---
-- ===========================================================================
-- 입출항 현황 재작성 — upa_port_call 배제 (2026-09-20)
--
-- 설계 근거: docs/11_선석제원_재설계_설계문서.md §21
--
-- ---------------------------------------------------------------------------
-- 왜 고치는가
--
--   `mart.port_call_overview` 는 portmis_vessel(신고)과 upa_port_call(관제 이력)을
--   FULL JOIN 해 왔다. upa_port_call 은 **사후 기록**이라 관제 화면의 '현재'를
--   만드는 데 쓰면 안 된다 — 출항 기록이 늦으면 이미 나간 배가 남고, 접안 시각도
--   실제가 아니라 사후 정리된 값이다.
--
--   역할 분리(§21):
--     portmis_vessel       입항 예정 · 배정 선석   (계획)
--     upa_vessel_position  실시간 위치            (현재)
--     upa_port_call        사후 이력              (배제)
--
--   그래서 이 뷰는 **신고 기준 입출항 현황**만 담는다. 실제 접안 여부는
--   mart.berth_occupancy_live(실시간)가, 현재 위치는 mart.vessel_latest_position
--   이 답한다.
--
-- ---------------------------------------------------------------------------
-- 이름
--
--   내용이 '항차 이력(port call)'이 아니라 '입출항 신고 현황'이 되므로
--   `mart.arrival_schedule` 로 바꾼다. `port_call_overview` 는 같은 내용을 보는
--   **호환 뷰**로 남긴다 — dashboard_current 와 프론트엔드가 그 이름으로 읽고
--   있어, 이름만 바꾸면 화면이 깨진다. 소비처를 옮긴 뒤 지우면 된다.
--
--   호환 뷰에는 port_call_id · io_vts_name 이 남는다. 둘 다 upa_port_call 에서
--   오던 값이라 이제 NULL 이다. 컬럼을 없애지 않는 이유는 소비처가 SELECT 로
--   지목하고 있어 빠지면 쿼리가 깨지기 때문이다.
-- ===========================================================================
CREATE VIEW mart.arrival_schedule AS
WITH pm_latest AS (
    -- 같은 항차가 신고 차수(최초/변경/최종)마다 반복된다. 선박당 최신 1건.
    SELECT DISTINCT ON (upper(btrim(callsgn)))
           upper(btrim(callsgn)) AS callsgn,
           entry_year, entry_count, entry_purpose_nm,
           origin_port_nm, prev_port_nm, next_port_nm, dest_port_nm,
           is_domestic_voyage, port_agency_label,
           arrival_at_utc, departure_at_utc, arrival_report_type,
           arrival_facility_cd, arrival_facility_sub_code, arrival_facility_nm
    FROM portmis_vessel
    WHERE nullif(btrim(callsgn), '') IS NOT NULL
    ORDER BY upper(btrim(callsgn)), arrival_at_utc DESC NULLS LAST
),
cargo_sum AS (
    SELECT upper(btrim(callsgn)) AS callsgn,
           count(*)                                        AS cargo_item_count,
           count(DISTINCT bl_no)                           AS bl_count,
           count(*) FILTER (WHERE dg_un_no IS NOT NULL)     AS dg_cargo_count,
           array_agg(DISTINCT dg_un_no) FILTER (WHERE dg_un_no IS NOT NULL) AS dg_un_nos,
           array_agg(DISTINCT cargo_name_raw) FILTER (WHERE cargo_name_raw IS NOT NULL) AS cargo_names
    FROM upa_cargo_manifest
    WHERE nullif(btrim(callsgn), '') IS NOT NULL
    GROUP BY upper(btrim(callsgn))
)
SELECT
    pm.callsgn, pm.entry_year, pm.entry_count, pm.entry_purpose_nm,
    pm.origin_port_nm, pm.prev_port_nm, pm.next_port_nm, pm.dest_port_nm,
    pm.is_domestic_voyage, pm.port_agency_label,
    pm.arrival_at_utc, pm.departure_at_utc,
    -- 신고 확정도. '최종'이 아니면 arrival_at_utc 는 예정 시각이다.
    pm.arrival_report_type,
    -- 배정 선석: 매핑이 있으면 우리 표기, 없으면 PORT-MIS 원문.
    COALESCE(m.berth_id, m.wharf_name, pm.arrival_facility_nm)::text AS facility_name,
    m.wharf_name AS assigned_wharf_name,
    m.berth_id   AS assigned_berth_id,
    cs.cargo_item_count, cs.bl_count, cs.dg_cargo_count, cs.dg_un_nos, cs.cargo_names
FROM pm_latest pm
LEFT JOIN portmis_facility_map m
       ON m.facility_cd       = pm.arrival_facility_cd
      AND m.facility_sub_code = pm.arrival_facility_sub_code
LEFT JOIN cargo_sum cs ON cs.callsgn = pm.callsgn;

-- ---------------------------------------------------------------------------
-- 2. mart.port_call_overview — 호환 뷰 (소비처 이전 전까지 유지)
--
-- port_call_id · io_vts_name 은 upa_port_call 에서 오던 값이라 NULL 이다.
-- 컬럼 자체를 없애면 소비처 SELECT 가 깨지므로 자리만 남긴다.
-- ---------------------------------------------------------------------------
COMMENT ON VIEW mart.arrival_schedule IS
    '입출항 신고 현황(PORT-MIS). 배정 선석은 portmis_facility_map 경유. 실제 접안은 mart.berth_occupancy_live 참조';

-- --- 7-2. mart.berth_occupancy · berth_audit · berth_facility_traffic ---
-- ===========================================================================
-- 선석 제원 감사 뷰 — PORT-MIS 배정 × 선석/부두 제원 × 선박 제원
--
-- 설계 근거: docs/11_선석제원_재설계_설계문서.md
-- 선행 조건: alembic upgrade head (0021 wharf/berth · 0022 portmis_facility_map)
--            + berth_seed_loader 적재
--
-- 이 파일은 mart_views.sql 과 **별도**다. 기존 뷰를 하나도 건드리지 않는다.
--
-- ---------------------------------------------------------------------------
-- 이 뷰들이 서 있는 전제
--
--   우리는 선석을 배정하지 않는다. PORT-MIS 가 이미 내린 배정을 받아서
--   **검증**한다. 그래서 하는 일은 후보를 고르는 것이 아니라, 남이 내린 결정에
--   물리적 모순이 없는지 보는 것이다.
--
--   ★ 조인은 portmis_facility_map 을 거친다
--     PORT-MIS 코드(MBU/01)와 UPA 부두현황 코드(MDU/02)는 3글자 접두 공간을
--     공유하지만 **배정이 다른 별개 레지스트리**다. 직접 조인하면 대부분
--     빗나가고 일부는 우연히 맞아 더 나쁘다. 그래서 PORT-MIS 원본에서 수집한
--     매핑표를 경유한다(§3).
--
--   ★ 붙지 않는 시설도 표에 남는다
--     portmis_facility_map 은 PORT-MIS 가 쓰는 계선시설을 **전부** 담는다
--     (정박지 제외). 못 붙인 것을 목록에서 지우면 감사에서 조용히 사라지기
--     때문이다. spec_basis 로 다섯 가지를 구분한다:
--
--   ★ 길이 게이트는 계류 방식을 본다
--     돌핀 계류는 선박이 구조물보다 긴 것이 정상이라(UTT부두: 안벽 80m 에
--     179m 선박) 안벽길이로 판정하면 전부 오탐이 된다. quay_structure 가
--     돌핀 계열이면 length_verdict='NOT_APPLICABLE' 로 둔다. 현재 118선석 중
--     30선석(돌핀 19 + 강관돌핀 11)이 해당한다.
--
--       BERTH            선석까지 특정 — 정확 판정
--       WHARF_WORST_CASE 부두까지만 — 최악값으로 보수 판정
--       KNOWN_GAP        제원 자료가 없음이 **확인됨**(gap_reason 참고). 조치 불가
--       UNMAPPED         아직 안 붙임 — **작업 대기열**. 0 이어야 정상
--       ANCHORAGE        정박지 — 안벽 제원으로 판정할 대상이 아님
--                        (upa_anchorage 와 대조해 가린다. 접두어 추측 금지)
--
--     KNOWN_GAP 과 UNMAPPED 를 섞으면 대기열이 줄지 않는다. 실측 예:
--     장생포호안은 입항 배정이 124건으로 안벽 시설 중 2위인데 세 출처
--     어디에도 제원이 없다 — 이건 고칠 수 없는 KNOWN_GAP 이다.
--
--   ★ 판정 입도가 배정마다 다르다
--     PORT-MIS 는 '자동차부두 02' 처럼 선석까지 특정하기도 하고 'SK3부두'
--     처럼 부두로만 등록되기도 한다. 그래서:
--
--       map.berth_id 있음 → 그 선석 제원으로 **정확 판정**
--       map.berth_id 없음 → 그 부두의 **최악값(MIN)**으로 보수 판정
--
--     어느 쪽으로 판정했는지는 spec_basis 컬럼에 남긴다. 소비처가 경고의
--     성격(확정 / 과잉 가능)을 구분할 수 있어야 하기 때문이다.
--
--   ★ 접안능력(DWT) 게이트는 넣지 않았다
--     berth.capacity_value 는 재화중량(DWT)이고 portmis_vessel.gross_tonnage 는
--     용적 기반 총톤수(GT)다. 차원이 다르고 환산 계수는 선종·화물에 따라 크게
--     달라 안전 판정 근거로 쓸 수 없다. 표시용으로만 내보낸다(capacity_note).
-- ===========================================================================
-- ---------------------------------------------------------------------------
-- 1. mart.berth_occupancy — PORT-MIS 배정을 점유 구간으로 본 뷰
--
-- 테이블이 아니라 뷰다. 배정을 우리가 만들지 않으므로, 별도 테이블로 복사하면
-- 같은 사실의 두 번째 사본이 생기고 원본과 어긋날 때 어느 쪽이 맞는지 답할 수
-- 없다.
--
-- ★ 입항 시설과 출항 시설이 다른 경우(shifting)
--   중간에 이안·재접안이 있었다는 뜻이다. 한 구간으로 뭉개면 안 되지만
--   전환 시점은 알 수 없다. 그래서 두 행으로 나누되 경계를 NULL 로 두고
--   is_shifted 로 표시한다 — 없는 정밀도를 지어내지 않는다.
-- ---------------------------------------------------------------------------
CREATE VIEW mart.berth_occupancy AS
WITH base AS (
    SELECT
        callsgn, vessel_name, entry_year, entry_count, gross_tonnage,
        arrival_facility_cd, arrival_facility_sub_code, arrival_facility_nm,
        departure_facility_cd, departure_facility_sub_code,
        arrival_at_utc, departure_at_utc,
        (departure_facility_cd IS NOT NULL
         AND arrival_facility_cd IS NOT NULL
         AND (departure_facility_cd, departure_facility_sub_code)
             IS DISTINCT FROM (arrival_facility_cd, arrival_facility_sub_code)
        ) AS is_shifted
    FROM portmis_vessel
    WHERE arrival_facility_cd IS NOT NULL
)
SELECT
    callsgn, vessel_name, entry_year, entry_count, gross_tonnage,
    'ARRIVAL'::text            AS leg,
    arrival_facility_cd        AS facility_cd,
    arrival_facility_sub_code  AS facility_sub_code,
    arrival_facility_nm        AS facility_name_reported,
    arrival_at_utc             AS occupied_from_utc,
    -- 이동이 있었으면 이 구간이 언제 끝났는지 모른다. NULL 이 정직하다.
    CASE WHEN is_shifted THEN NULL ELSE departure_at_utc END AS occupied_to_utc,
    is_shifted
FROM base
UNION ALL
SELECT
    callsgn, vessel_name, entry_year, entry_count, gross_tonnage,
    'DEPARTURE'::text, departure_facility_cd, departure_facility_sub_code,
    NULL, NULL, departure_at_utc, TRUE
FROM base
WHERE is_shifted;


-- ---------------------------------------------------------------------------
-- 2. mart.berth_audit — 길이·수심 게이트
--
-- 판정 어휘는 기존 mart.berth_draught_check 와 맞춘다(OK / MARGINAL /
-- NOT_ALLOWED / UNKNOWN). 다만 이쪽은 upa_port_call 이 아니라 portmis_vessel 을
-- 근거로 삼는다 — 관제 데이터의 실시간성 문제로 port_call 을 배제했다.
--
-- UKC(Under Keel Clearance)는 통상 관행값인 흘수의 10% 를 기본으로 둔다.
-- 법정 수치가 아니며 터미널·항만별로 다르다 — 울산항 각 터미널 운영규정을
-- 입수해 확정해야 한다(미확보, 한계로 보고).
-- ---------------------------------------------------------------------------
CREATE VIEW mart.berth_audit AS
WITH tide AS (
    -- 가용수심 = 해도기준면 수심 + 조위. 조위를 빼먹으면 만조에만 접안 가능한
    -- 배를 영구 불가로 오판한다.
    SELECT tide_level_cm FROM tide_obs ORDER BY observed_at_utc DESC LIMIT 1
),
occ AS (
    SELECT * FROM mart.berth_occupancy WHERE leg = 'ARRIVAL'
),
joined AS (
    SELECT
        o.*,
        m.wharf_name, m.berth_id, m.match_level, m.confidence,
        w.port_name, w.berth_count, w.spec_spread_flag,
        -- 선석이 특정되면 그 선석 값, 아니면 부두 최악값. COALESCE 순서가
        -- 곧 판정 정책이다(§6).
        -- ★ 수심은 선석 값을 쓰되, 그 부두가 '웹이 얕은 선석을 누락'으로 확인된
        --   경우(w.depth_floored)에는 **부두 최저수심**을 쓴다.
        --   어느 선석이 얕은지 알 수 없으므로 선석 단위 정밀도를 포기하고
        --   보수적으로 판정한다. 실측: 2부두는 웹이 3선석 모두 12m 라 적지만
        --   해수청 원본은 9~12m 다 — 12m 로 판정하면 착저 위험을 놓친다.
        CASE
            WHEN COALESCE(w.depth_floored, FALSE)
                THEN LEAST(COALESCE(b.water_depth_m, w.min_water_depth_m),
                           w.min_water_depth_m)
            ELSE COALESCE(b.water_depth_m, w.min_water_depth_m)
        END AS spec_depth_m,
        COALESCE(w.depth_floored, FALSE) AS depth_is_wharf_floor,
        COALESCE(b.length_m,       w.min_length_m)      AS spec_length_m,
        -- 선석 길이가 미상일 때의 상한. 웹이 부두 총연장을 선석마다 복사해 싣는
        -- 경우가 많아(일반부두: 7개 선석 모두 '679m') 선석 길이를 확정할 수 없는
        -- 부두가 상당수다. 그래도 'LOA > 부두 총연장'이면 확실히 불가라,
        -- 판정을 통째로 포기하지 않고 이 한 가지는 잡아낸다.
        w.total_quay_length_m,
        b.quay_structure,
        COALESCE(b.capacity_value, w.min_capacity_dwt)  AS spec_min_dwt,
        w.max_capacity_dwt,
        -- 판정이 무엇에 근거했는지. UNKNOWN 이 나왔을 때 **왜** 모르는지가
        -- 구분돼야 한다 — 고칠 수 있는 것과 자료가 없는 것은 다르다.
        CASE
            WHEN b.berth_id IS NOT NULL            THEN 'BERTH'
            WHEN m.wharf_name IS NOT NULL          THEN 'WHARF_WORST_CASE'
            WHEN m.match_level = 'KNOWN_GAP'       THEN 'KNOWN_GAP'
            WHEN m.match_level = 'UNMAPPED'        THEN 'UNMAPPED'
            WHEN a.facility_code IS NOT NULL       THEN 'ANCHORAGE'
            ELSE 'NOT_IN_REGISTRY'
        END AS spec_basis,
        m.gap_reason,
        vs.loa_m, vs.draught_m,
        (SELECT tide_level_cm FROM tide) / 100.0 AS tide_m
    FROM occ o
    -- 매핑에 없는 시설은 안 붙고 아래에서 UNKNOWN 이 된다
    -- ("모르는 것을 안전으로 간주하지 않는다"). 정박지·호안이 여기 해당하고,
    -- UPA 웹 부두현황에 없는 부두(북신항 액체·에너지부두 등)도 마찬가지다.
    LEFT JOIN portmis_facility_map m
           ON m.facility_cd       = o.facility_cd
          AND m.facility_sub_code = o.facility_sub_code
    LEFT JOIN wharf w ON w.wharf_name = m.wharf_name
    LEFT JOIN berth b ON b.berth_id   = m.berth_id
    LEFT JOIN vessel_spec vs
           ON upper(btrim(vs.callsgn)) = upper(btrim(o.callsgn))
    -- 정박지 판별은 **추측하지 않고 정박지 레지스트리와 대조한다.**
    -- 코드 접두어로 가르면 틀린다 — MQP-01 은 '미포부두 01' 로 정박지가 아니라
    -- 안벽이고, MQ* 를 통째로 정박지 취급하면 그 배정이 조용히 빠진다.
    -- upa_anchorage.facility_code 는 'WAE-02' 결합형이다.
    LEFT JOIN (SELECT DISTINCT facility_code FROM upa_anchorage) a
           ON a.facility_code = o.facility_cd || '-' || lpad(o.facility_sub_code, 2, '0')
)
SELECT
    callsgn, vessel_name, entry_year, entry_count,
    facility_cd, facility_sub_code, facility_name_reported,
    wharf_name, berth_id, port_name,
    occupied_from_utc, occupied_to_utc, is_shifted,
    spec_basis, match_level, confidence, gap_reason,
    loa_m, spec_length_m,
    draught_m, spec_depth_m, depth_is_wharf_floor, tide_m,
    round((spec_depth_m + COALESCE(tide_m, 0))::numeric, 2) AS available_depth_m,

    -- 길이 게이트 — LOA 와 안벽길이 모두 m 라 직접 비교된다.
    --
    -- 선석 길이가 확정된 경우에만 정상 판정한다. 미상일 때는 부두 총연장으로
    -- **상한만** 본다: LOA 가 총연장보다 길면 어느 선석에도 못 붙으므로
    -- NOT_ALLOWED 가 확실하다. 그 외에는 UNKNOWN 이다 — 총연장 안에 든다고
    -- 해서 개별 선석에 든다는 보장은 없으므로 OK 라고 말하지 않는다.
    CASE
        WHEN loa_m IS NULL                          THEN 'UNKNOWN'
        -- ★ 돌핀 계류에는 길이 게이트를 적용하지 않는다.
        --   돌핀(dolphin)은 이격된 계선주에 배를 매는 방식이라 **선박이 구조물보다
        --   긴 것이 정상**이다. UTT부두는 안벽 80m 인데 179m 선박이 정상 접안한다.
        --   안벽식 기준을 그대로 대면 전부 NOT_ALLOWED 로 뜬다(실측 오탐).
        --   실제 제약은 돌핀 간격·계류색 배치라 안벽길이로는 판정할 수 없다.
        WHEN quay_structure LIKE '%돌핀%'            THEN 'NOT_APPLICABLE'
        WHEN spec_length_m IS NOT NULL THEN
            CASE
                WHEN loa_m > spec_length_m       THEN 'NOT_ALLOWED'
                WHEN loa_m > spec_length_m * 0.9 THEN 'MARGINAL'
                ELSE 'OK'
            END
        WHEN total_quay_length_m IS NOT NULL
             AND loa_m > total_quay_length_m        THEN 'NOT_ALLOWED'
        ELSE 'UNKNOWN'
    END AS length_verdict,

    -- 길이 판정이 무엇에 근거했는지. 소비처가 UNKNOWN 의 이유를 알아야 한다.
    CASE
        WHEN quay_structure LIKE '%돌핀%'      THEN 'DOLPHIN_MOORING'
        WHEN spec_length_m IS NOT NULL       THEN 'BERTH_LENGTH'
        WHEN total_quay_length_m IS NOT NULL THEN 'WHARF_TOTAL_UPPER_BOUND'
        ELSE 'NONE'
    END AS length_basis,
    quay_structure,

    -- 수심 게이트 — 가용수심(수심+조위) 대비 흘수 + UKC 10%
    CASE
        WHEN draught_m IS NULL OR spec_depth_m IS NULL             THEN 'UNKNOWN'
        WHEN spec_depth_m + COALESCE(tide_m, 0) <= draught_m       THEN 'NOT_ALLOWED'
        WHEN spec_depth_m + COALESCE(tide_m, 0) <  draught_m * 1.1 THEN 'MARGINAL'
        ELSE 'OK'
    END AS draught_verdict,

    -- 접안능력은 판정하지 않는다(DWT-GT 차원 불일치). 근거만 내보낸다.
    format('접안능력 %s~%s DWT / 선박 GT %s (DWT-GT 직접비교 불가)',
           spec_min_dwt, max_capacity_dwt, gross_tonnage) AS capacity_note,

    -- 부두 최악값으로 판정했고 그 부두의 선석 제원이 서로 다르면, 실제 배정
    -- 선석에 따라 판정이 달라질 수 있다(과잉 경고 가능). 소비처가 경고의
    -- 성격을 구분할 수 있도록 표시한다.
    (spec_basis = 'WHARF_WORST_CASE' AND COALESCE(spec_spread_flag, FALSE)) AS verdict_may_be_pessimistic
FROM joined;
COMMENT ON VIEW mart.berth_occupancy IS
    'PORT-MIS 배정을 점유 구간으로 본 뷰. 테이블로 복사하지 않는다 — 배정의 정본은 portmis_vessel이다';
COMMENT ON VIEW mart.berth_audit IS
    'PORT-MIS 배정의 길이·수심 타당성 감사. 선석이 특정되면 정확 판정, 아니면 부두 최악값으로 보수 판정(spec_basis 참고)';


-- ---------------------------------------------------------------------------
-- 3. mart.berth_facility_traffic — 계선시설별 배정 건수와 감사 가능 여부
--
-- **뷰다. 매핑표에 배정 건수를 저장하지 않는다.**
--   그건 portmis_vessel 에서 파생되는 통계지 매핑 사실이 아니고, 수집이
--   진행될수록 저장해 둔 값은 낡는다. portmis_facility_map 은 정적 시드이므로
--   휘발성 수치를 담으면 안 된다 — 같은 이유로 berth_assignment 도 테이블이
--   아니라 뷰로 두었다(§11).
--
-- 용도: 제원 공백의 **우선순위**. 배정이 없는 시설은 제원이 비어 있어도 감사에
-- 아무 영향이 없다. 이 뷰를 arrival_count 내림차순으로 보면 무엇부터 손볼지
-- 바로 나온다.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.berth_facility_traffic AS
SELECT
    p.arrival_facility_cd                       AS facility_cd,
    p.arrival_facility_sub_code                 AS facility_sub_code,
    max(p.arrival_facility_nm)                  AS facility_nm,
    count(*)                                    AS arrival_count,
    m.wharf_name, m.berth_id,
    COALESCE(m.match_level,
             CASE WHEN a.facility_code IS NOT NULL THEN 'ANCHORAGE'
                  ELSE 'NOT_IN_REGISTRY' END)   AS match_level,
    m.gap_reason,
    -- 정박지는 안벽 제원이 아니라 **톤급 제한**으로 판정해야 한다
    -- (upa_anchorage.remark: '3만톤 이하' · '2천톤급이하' 등). 아직 판정 뷰는
    -- 없고 근거만 노출한다 — §18 참고.
    a.remark                                    AS anchorage_limit_raw
FROM portmis_vessel p
LEFT JOIN portmis_facility_map m
       ON m.facility_cd       = p.arrival_facility_cd
      AND m.facility_sub_code = p.arrival_facility_sub_code
LEFT JOIN (SELECT facility_code, min(remark) AS remark
           FROM upa_anchorage GROUP BY facility_code) a
       ON a.facility_code = p.arrival_facility_cd || '-'
                            || lpad(p.arrival_facility_sub_code, 2, '0')
WHERE p.arrival_facility_cd IS NOT NULL
GROUP BY p.arrival_facility_cd, p.arrival_facility_sub_code,
         m.wharf_name, m.berth_id, m.match_level, m.gap_reason, a.facility_code, a.remark;

COMMENT ON VIEW mart.berth_facility_traffic IS
    '계선시설별 입항 배정 건수 + 감사 가능 여부. 제원 공백의 우선순위를 정하는 용도. 건수는 저장하지 않고 매번 센다';

-- --- 7-3. mart.anchorage_limit · anchorage_audit ---
-- ===========================================================================
-- 정박지 감사 뷰 — PORT-MIS 정박지 배정 × 톤급 제한
--
-- 설계 근거: docs/11_선석제원_재설계_설계문서.md §18
-- 선행 조건: upa_anchorage 적재(기존 파이프라인) + portmis_vessel
--
-- berth_audit_views.sql 과 **별도 파일**이다. 안벽 감사와 판정 축이 다르다 —
-- 안벽은 길이·수심(m), 정박지는 톤급이다.
--
-- ---------------------------------------------------------------------------
-- 왜 필요한가
--
--   PORT-MIS 배정의 절반가량이 정박지다(실측: 고유 입항 698건 중 336건).
--   지금까지 이쪽은 판정 없이 지나가고 있었다 — 감사 대상의 절반이 비어 있었다.
--
--   다행히 정박지는 안벽보다 판정이 단순하다. `upa_anchorage.remark` 에
--   톤급 제한이 있고, PORT-MIS 가 주는 `gross_tonnage` 와 **같은 톤 축**이다.
--   (안벽의 접안능력 DWT 와 선박 GT 는 차원이 달라 환산이 불가능했다.)
--
-- ---------------------------------------------------------------------------
-- ★ 단위 = 총톤수(G/T) — 법령 별표로 확인됨 (2026-09-20)
--
--   `remark` 원문에는 단위가 없지만, 정박지 지정의 근거 법령 별표가 컬럼명을
--   명시한다:
--       '[별표 1] 주요항만시설 … 정 박 지 … **정박능력(G/T)**'
--       '[별표 1] 정박지 ① 작업 및 대기 … **제한톤수(G/T), 척**'
--   코드 체계도 같다(별표의 'WAJ-02' ↔ upa_anchorage 의 'WAE-02').
--   따라서 portmis_vessel.gross_tonnage 와 **같은 축**이며 직접 비교된다.
--
--   ※ 다만 법령 별표에는 UPA remark 에 없는 부가 제한이 있다 —
--     '단, 길이 200m이하이고, 만재흘수 7.2m이하' 같은 LOA·흘수 조건.
--     즉 이 뷰의 톤급 판정은 **법령 제한의 일부만** 본다. 울산항 별표는
--     스캔 이미지라 텍스트 추출이 안 돼 아직 반영하지 못했다(OCR 필요).
-- ===========================================================================
-- ---------------------------------------------------------------------------
-- 톤 표기 → 숫자. '1만톤'=10000 · '2천톤급'=2000 · '500톤'=500
--
-- 실제 값 20종 전부에 대해 파싱 결과를 대조했다(설계문서 §18).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mart.ton_value(txt text) RETURNS numeric AS $$
    SELECT CASE
        WHEN txt ~ '만' THEN ((regexp_match(txt, '(\d+(?:\.\d+)?)\s*만'))[1])::numeric * 10000
        WHEN txt ~ '천' THEN ((regexp_match(txt, '(\d+(?:\.\d+)?)\s*천'))[1])::numeric * 1000
        ELSE ((regexp_match(txt, '(\d+(?:\.\d+)?)'))[1])::numeric
    END;
$$ LANGUAGE sql IMMUTABLE;

-- ---------------------------------------------------------------------------
-- 1. mart.anchorage_limit — 정박지별 톤급 제한
--
-- upa_anchorage 는 폴리곤 꼭짓점마다 행이 있어(정박지 하나에 최대 41행)
-- DISTINCT 로 접는다. remark 는 같은 정박지 안에서 동일하다.
--
-- ★ 표기가 세 가지고 **부등호 방향이 서로 다르다.**
--     '3만톤 이하'            → 상한만
--     '2만톤 이상' · '5만톤 초과' → **하한만** (E3·B3-2)
--     '2만톤 초과 ~ 5만톤 이하'  → 하한 + 상한
--   전부 '이하'로 뭉뚱그리면 E3 판정이 정확히 뒤집힌다.
-- ---------------------------------------------------------------------------
CREATE VIEW mart.anchorage_limit AS
WITH base AS (
    SELECT DISTINCT facility_code, anchorage_name, anchorage_type, remark
    FROM upa_anchorage
    WHERE nullif(btrim(remark), '') IS NOT NULL
),
split AS (
    SELECT *,
           split_part(remark, '~', 1)               AS p1,
           nullif(btrim(split_part(remark, '~', 2)), '') AS p2
    FROM base
)
SELECT
    facility_code, anchorage_name, anchorage_type,
    remark AS limit_raw,
    CASE WHEN p2 IS NOT NULL            THEN mart.ton_value(p1)
         WHEN remark ~ '이상|초과'       THEN mart.ton_value(remark)
    END AS min_ton,
    CASE WHEN p2 IS NOT NULL            THEN mart.ton_value(p2)
         WHEN remark ~ '이상|초과'       THEN NULL
         ELSE mart.ton_value(remark)
    END AS max_ton,
    -- 법령 별표의 컬럼명이 '정박능력(G/T)' · '제한톤수(G/T)' 다(2026-09-20 확인).
    -- portmis_vessel.gross_tonnage 와 같은 축이라 직접 비교된다.
    'GT'::text AS unit
FROM split;

-- ---------------------------------------------------------------------------
-- 2. mart.anchorage_audit — 정박지 배정의 톤급 적합성
--
-- 판정:
--   OK           제한 범위 안
--   NOT_ALLOWED  상한 초과 — 그 정박지에 댈 수 없는 크기
--   BELOW_MIN    하한 미달 — 안전 위험이 아니라 **지정 위반**이다.
--                (E3 는 2만톤 이상 전용이라 소형선이 오면 자리를 잘못 쓴 것)
--                NOT_ALLOWED 와 섞지 않는다 — 대응이 다르다.
--   UNKNOWN      총톤수 또는 제한 미상
--
-- 정박지 수심은 upa_anchorage 에 없다. 그래서 흘수 게이트는 하지 않는다 —
-- 없는 값을 지어내지 않는다.
-- ---------------------------------------------------------------------------
CREATE VIEW mart.anchorage_audit AS
WITH occ AS (
    -- 같은 항차가 신고 차수마다 다시 들어오므로 입항건 단위로 접는다.
    -- (raw 레코드를 그대로 세면 실측상 1.5배 부풀려진다 — §18)
    SELECT DISTINCT ON (callsgn, entry_year, entry_count)
           callsgn, entry_year, entry_count, vessel_name, gross_tonnage,
           ship_kind_nm, entry_purpose_nm, arrival_report_type,
           arrival_facility_cd, arrival_facility_sub_code, arrival_facility_nm,
           arrival_at_utc, departure_at_utc
    FROM portmis_vessel
    WHERE arrival_facility_cd IS NOT NULL
    ORDER BY callsgn, entry_year, entry_count, arrival_at_utc DESC NULLS LAST
)
SELECT
    o.callsgn, o.vessel_name, o.entry_year, o.entry_count,
    o.ship_kind_nm, o.entry_purpose_nm, o.arrival_report_type,
    o.arrival_facility_cd  AS facility_cd,
    o.arrival_facility_sub_code AS facility_sub_code,
    o.arrival_facility_nm  AS facility_name_reported,
    l.anchorage_name, l.anchorage_type,
    o.arrival_at_utc, o.departure_at_utc,
    o.gross_tonnage,
    l.limit_raw, l.min_ton, l.max_ton, l.unit,
    CASE
        WHEN o.gross_tonnage IS NULL                     THEN 'UNKNOWN'
        WHEN l.min_ton IS NULL AND l.max_ton IS NULL     THEN 'UNKNOWN'
        WHEN l.max_ton IS NOT NULL
             AND o.gross_tonnage > l.max_ton             THEN 'NOT_ALLOWED'
        WHEN l.min_ton IS NOT NULL
             AND o.gross_tonnage < l.min_ton             THEN 'BELOW_MIN'
        ELSE 'OK'
    END AS tonnage_verdict,

    -- ★ 위반의 **확정도**. PORT-MIS 는 신고 데이터라 '최초/변경' 은 예정이고
    --   '최종' 이라야 확정에 가깝다. 둘을 같은 경고로 내보내면 대응이 섞인다.
    --
    --     PLANNED   입항 전 신고 단계 — 조정 가능. **이 시점 경고가 가장 쓸모 있다**
    --     REPORTED  최종 신고까지 초과 — 관제 확인 대상
    --
    --   실측(2026-09-20): E1 초과 5건은 전부 최초·변경이고 최종 신고가 없었다.
    --   즉 '신고 기준 초과'이지 실제 투묘 위반으로 확정된 것은 아니다.
    CASE
        WHEN o.arrival_report_type = '최종' THEN 'REPORTED'
        ELSE 'PLANNED'
    END AS verdict_confidence
FROM occ o
-- 정박지 판별은 코드 접두어로 추측하지 않는다 — 레지스트리와 대조한다.
-- upa_anchorage.facility_code 가 'WAE-02' 결합형이다.
JOIN mart.anchorage_limit l
  ON l.facility_code = o.arrival_facility_cd || '-'
                       || lpad(o.arrival_facility_sub_code, 2, '0');

COMMENT ON VIEW mart.anchorage_limit IS
    '정박지별 톤급 제한(upa_anchorage.remark 파싱). 이상/이하/범위 세 표기를 구분한다';
COMMENT ON VIEW mart.anchorage_audit IS
    'PORT-MIS 정박지 배정의 톤급 적합성(총톤수 G/T 기준, 법령 별표로 확인). verdict_confidence 로 예정/확정을 구분한다';

-- --- 7-4. mart.approval_candidates ---
-- ===========================================================================
-- mart.approval_candidates — 관제사 승인 대기 후보 (입항 예정 + 선석 미배정)
--
-- ---------------------------------------------------------------------------
-- 이 파일이 왜 뒤늦게 생겼나 (2026-09-21)
-- ---------------------------------------------------------------------------
--   이 뷰는 DB 에는 있었지만 **저장소 어디에도 DDL 이 없었다.** 누군가 psql 에서
--   직접 만들고 커밋하지 않은 것이다. 저장소만으로 DB 를 재구성하면 이 뷰는
--   생기지 않았다.
--
--   실제로 사고가 났다. 2026-09-21 에 mart.cargo_msds 를 재작성하면서
--   `DROP VIEW ... CASCADE` 를 했더니 이 뷰가 의존 관계로 함께 삭제됐고,
--   복구할 원본이 저장소에 없었다. 살아 있는 DB 의 pg_get_viewdef() 출력으로
--   되살린 것이 아래 정의다 — DB 가 먼저 죽었으면 영영 잃을 뻔했다.
--
--   ※ docs/13 §2-C 는 이 뷰의 필터 리터럴이 mojibake('%������%') 라고 적었는데
--     그건 **사실이 아니다.** client_encoding 을 UTF8 과 EUC_KR 로 각각 바꿔
--     두 번 확인한 결과 저장된 정의는 정상적인 '%정박지%' 였다(2026-09-21).
--     문서 작성 당시 조회 도구가 한글을 못 읽은 것이다. 필터는 잘 동작한다
--     (실측 29행 반환). 반면 "DDL 이 저장소에 없다"는 지적은 사실이었다.
--
-- ---------------------------------------------------------------------------
-- 무엇을 뽑는가
-- ---------------------------------------------------------------------------
--   온산항(port_agency_cd='820')에 **내일 이후 입항 예정**인 액체화물선 중,
--   아직 출항하지 않았고, 계선시설이 비었거나 '정박지'로 배정된 건.
--
--   즉 "선석이 아직 정해지지 않아 관제사 판단이 필요한 배"다. 실측(2026-09-21)
--   기준 portmis_vessel 550행의 배정 분포가 아래와 같아, 이 뷰의 대상이
--   전체의 절반을 넘는다:
--       정박지 배정  285행 (51.8%)
--       선석 배정    219행 (39.8%)
--       미배정        46행 ( 8.4%)
--
--   draught 는 upa_vessel_position 최신 1건에서 가져오되, 없으면 3.0m 로
--   가정하고 draught_is_estimated=true 로 표시한다 — 흘수가 없다고 후보에서
--   빼면 AIS 정적신호를 안 보내는 배가 통째로 화면에서 사라지기 때문이다.
--   (추정값을 게이트에 그대로 쓰면 안 된다. 표시용이다.)
--
-- 선행 조건: mart.cargo_msds (cargo_msds_v2.sql), upa_vessel_position, portmis_vessel
-- ===========================================================================
CREATE VIEW mart.approval_candidates AS
SELECT
    pm.id                       AS portmis_vessel_id,
    pm.callsgn,
    pm.vessel_name,
    pm.ship_kind_category,
    pm.is_liquid_cargo_vessel,
    pm.arrival_at_utc,
    pm.departure_sched_utc,
    pm.arrival_facility_nm,
    pm.gross_tonnage,
    pm.agency_name,
    pos.latitude,
    pos.longitude,
    pos.sog,
    pos.nav_status_code,
    pos.received_at_utc         AS position_last_seen_utc,
    COALESCE(pos.draught, 3.0::double precision) AS draught,
    pos.draught IS NULL         AS draught_is_estimated,
    cm.cargo_name_raw,
    cm.dg_un_no,
    cm.cargo_basis,
    cm.chem_id,
    cm.cas_no,
    cm.flash_point_celsius,
    cm.imdg_class,
    cm.signal_word,
    cm.msds_matched
FROM portmis_vessel pm
LEFT JOIN LATERAL (
    SELECT p.latitude, p.longitude, p.sog, p.nav_status_code,
           p.received_at_utc, p.draught
    FROM upa_vessel_position p
    WHERE upper(btrim(p.callsgn)) = upper(btrim(pm.callsgn))
    ORDER BY p.received_at_utc DESC NULLS LAST
    LIMIT 1
) pos ON true
LEFT JOIN mart.cargo_msds cm
       ON cm.callsgn = upper(btrim(pm.callsgn))
WHERE pm.port_agency_cd = '820'
  AND pm.is_liquid_cargo_vessel
  AND pm.departure_at_utc IS NULL
  AND (pm.arrival_facility_nm IS NULL OR pm.arrival_facility_nm LIKE '%정박지%')
  AND pm.arrival_at_utc >= (
        (date_trunc('day', (now() AT TIME ZONE 'Asia/Seoul')) + interval '1 day')
        AT TIME ZONE 'Asia/Seoul');

COMMENT ON VIEW mart.approval_candidates IS
    '온산항 입항예정 액체화물선 중 선석 미배정(정박지 또는 공란) 건 — 관제사 승인 후보. DDL 은 approval_candidates.sql(2026-09-21 저장소 편입)';

