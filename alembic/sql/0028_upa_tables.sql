-- 0028: data-pipeline 이 auto_create 로 만들던 UPA 표 5종을 Alembic 소유로 가져온다.
-- 2026-09-24 로컬 개발 DB(smartport-postgres-dev)에서 pg_dump -s 로 뜬 정의 그대로다.
-- 이미 표가 있는 DB 에서는 CREATE 가 아무 일도 하지 않는다(IF NOT EXISTS).
-- 유니크 인덱스는 data-pipeline upa_loader.TABLE_MAP 의 적재 키만 만든다 — 이름도 로컬과 같게.

CREATE TABLE IF NOT EXISTS public.upa_anchorage (
    anchorage_name text,
    facility_code text,
    index_no bigint,
    latitude double precision,
    longitude double precision,
    radius_m double precision,
    remark text,
    anchorage_type text,
    title_yn text,
    seq_no bigint,
    source_system text,
    source_table text,
    collected_at_utc timestamp with time zone,
    quality_flag text,
    is_synthetic boolean,
    record_uid text
);
COMMENT ON TABLE public.upa_anchorage IS 'UPA 정박지(닻을 내리는 구역) 마스터. 톤수 구간별 정박지 배정 규칙과 연결됨';
COMMENT ON COLUMN public.upa_anchorage.anchorage_name IS '정박지명';
COMMENT ON COLUMN public.upa_anchorage.facility_code IS '시설코드(UPA 원문)';
COMMENT ON COLUMN public.upa_anchorage.index_no IS '원문 API 인덱스 번호';
COMMENT ON COLUMN public.upa_anchorage.latitude IS '정박지 중심 위도';
COMMENT ON COLUMN public.upa_anchorage.longitude IS '정박지 중심 경도';
COMMENT ON COLUMN public.upa_anchorage.radius_m IS '정박지 반경(m)';
COMMENT ON COLUMN public.upa_anchorage.remark IS '비고';
COMMENT ON COLUMN public.upa_anchorage.anchorage_type IS '정박지 유형(톤수구간 등 배정 규칙과 연결)';
COMMENT ON COLUMN public.upa_anchorage.title_yn IS '제목행 여부(원문 API 표기, Y/N)';
COMMENT ON COLUMN public.upa_anchorage.seq_no IS '표시 순번';
COMMENT ON COLUMN public.upa_anchorage.source_system IS '데이터 출처 시스템명(UPA)';
COMMENT ON COLUMN public.upa_anchorage.source_table IS '출처 원본 API명(getGisBaseAnchrgDtlInfo)';
COMMENT ON COLUMN public.upa_anchorage.collected_at_utc IS '수집 시각';
COMMENT ON COLUMN public.upa_anchorage.quality_flag IS '데이터 품질 플래그';
COMMENT ON COLUMN public.upa_anchorage.is_synthetic IS '합성 데이터 여부';
COMMENT ON COLUMN public.upa_anchorage.record_uid IS '행 고유키(UPSERT용)';
CREATE TABLE IF NOT EXISTS public.upa_cargo_manifest (
    record_uid text,
    port_code text,
    ptent_yr text,
    voyage_no text,
    callsgn text,
    vessel_name text,
    vessel_type_name text,
    vessel_nationality_code text,
    vessel_nationality_name text,
    mrn_no text,
    bl_no text,
    master_bl_no text,
    io_se_code text,
    io_se_name text,
    facility_name text,
    cargo_se_name text,
    cargo_name_raw text,
    dg_un_no text,
    cargo_basis text,
    package_type_name text,
    unload_method_name text,
    vol_ton_unit_name text,
    vol_ton double precision,
    weight_ton double precision,
    vol_size double precision,
    weight_size double precision,
    bulk_vol_size double precision,
    bulk_weight_size double precision,
    container_count double precision,
    pod_name text,
    pol_name text,
    ldud_port_name text,
    last_dest_port_name text,
    arrival_at_utc timestamp with time zone,
    customs_progress_status_name text,
    source_system text,
    source_table text,
    collected_at_utc timestamp with time zone,
    quality_flag text,
    is_synthetic boolean,
    chem_id character varying(20)
);
COMMENT ON TABLE public.upa_cargo_manifest IS '화물 신고 정보. UPA 통합화물 API(getIntgCagInfo)가 업체코드(bzentyCd) 미확보로 영구 호출 불가라, 전 행이 합성(가짜) 데이터임(is_synthetic=true). callsgn/선종만 실제 PORT-MIS 데이터 기반이고 화물명·수량·부두 등은 생성 로직이 채운 것';
COMMENT ON COLUMN public.upa_cargo_manifest.record_uid IS '행 고유키(UPSERT 키). bzentyCd 미확보로 자연키를 못 써서 이 방식 유지';
COMMENT ON COLUMN public.upa_cargo_manifest.port_code IS '항만 코드(KRUSN 고정)';
COMMENT ON COLUMN public.upa_cargo_manifest.ptent_yr IS '입항연도(합성값)';
COMMENT ON COLUMN public.upa_cargo_manifest.voyage_no IS '항차번호(합성값)';
COMMENT ON COLUMN public.upa_cargo_manifest.callsgn IS '호출부호(PORT-MIS 실선박 기반 — 이 값만 실제)';
COMMENT ON COLUMN public.upa_cargo_manifest.vessel_name IS '선박명(실선박 기반)';
COMMENT ON COLUMN public.upa_cargo_manifest.vessel_type_name IS '선종명(추정 포함, "(추정)" 접미 표기)';
COMMENT ON COLUMN public.upa_cargo_manifest.vessel_nationality_code IS '선박 국적 코드';
COMMENT ON COLUMN public.upa_cargo_manifest.vessel_nationality_name IS '선박 국적 명칭';
COMMENT ON COLUMN public.upa_cargo_manifest.mrn_no IS '화물관리번호(합성값)';
COMMENT ON COLUMN public.upa_cargo_manifest.bl_no IS '선화증권(B/L)번호(합성값). 정상 케이스는 BL/위반 시나리오 케이스는 BL9로 시작해 구분됨';
COMMENT ON COLUMN public.upa_cargo_manifest.master_bl_no IS '마스터 B/L번호(합성값)';
COMMENT ON COLUMN public.upa_cargo_manifest.io_se_code IS '수입/수출 구분 코드(I/O)';
COMMENT ON COLUMN public.upa_cargo_manifest.io_se_name IS '수입/수출 구분 명칭';
COMMENT ON COLUMN public.upa_cargo_manifest.facility_name IS '접안 부두명(합성값 — 이 배의 실제 위치(upa_port_call.facility_name)와 무관하게 배정되어 있어 다를 수 있음, 알려진 한계)';
COMMENT ON COLUMN public.upa_cargo_manifest.cargo_se_name IS '화물 구분("액체"/"일반")';
COMMENT ON COLUMN public.upa_cargo_manifest.cargo_name_raw IS '화물명 원문(합성, 그 배의 실선종 계열에 맞춰 그럴듯하게 배정)';
COMMENT ON COLUMN public.upa_cargo_manifest.dg_un_no IS '위험물 UN번호(합성이지만 IMDG DGL 참조표로 화학적 교차검증됨 — MSDS 조인키)';
COMMENT ON COLUMN public.upa_cargo_manifest.cargo_basis IS '화물 배정 근거("PORT-MIS 실선종 기반" 등, 이 값으로 추정 여부를 구분)';
COMMENT ON COLUMN public.upa_cargo_manifest.package_type_name IS '포장형태("벌크"/"포장")';
COMMENT ON COLUMN public.upa_cargo_manifest.unload_method_name IS '하역방식("펌프"/"크레인")';
COMMENT ON COLUMN public.upa_cargo_manifest.vol_ton_unit_name IS '부피/중량 단위명';
COMMENT ON COLUMN public.upa_cargo_manifest.vol_ton IS '부피(톤, 합성값)';
COMMENT ON COLUMN public.upa_cargo_manifest.weight_ton IS '중량(톤, 합성값)';
COMMENT ON COLUMN public.upa_cargo_manifest.vol_size IS '부피 상세(대부분 NULL)';
COMMENT ON COLUMN public.upa_cargo_manifest.weight_size IS '중량 상세(대부분 NULL)';
COMMENT ON COLUMN public.upa_cargo_manifest.bulk_vol_size IS '벌크 부피(액체화물, weight_ton*1.1 근사)';
COMMENT ON COLUMN public.upa_cargo_manifest.bulk_weight_size IS '벌크 중량(액체화물)';
COMMENT ON COLUMN public.upa_cargo_manifest.container_count IS '컨테이너 개수(해당 없음 — 대부분 NULL)';
COMMENT ON COLUMN public.upa_cargo_manifest.pod_name IS '양하항(Port Of Discharge)';
COMMENT ON COLUMN public.upa_cargo_manifest.pol_name IS '적하항(Port Of Loading)';
COMMENT ON COLUMN public.upa_cargo_manifest.ldud_port_name IS '양적하항(미사용, NULL)';
COMMENT ON COLUMN public.upa_cargo_manifest.last_dest_port_name IS '최종목적항(미사용, NULL)';
COMMENT ON COLUMN public.upa_cargo_manifest.arrival_at_utc IS '입항일시(합성값)';
COMMENT ON COLUMN public.upa_cargo_manifest.customs_progress_status_name IS '통관 진행상태명(합성값)';
COMMENT ON COLUMN public.upa_cargo_manifest.source_system IS '데이터 출처 시스템명("UPA_SYNTHETIC")';
COMMENT ON COLUMN public.upa_cargo_manifest.source_table IS '출처 표기("IntgCagInfo(SYNTHETIC)") — 실제 API 호출 결과가 아님을 명시';
COMMENT ON COLUMN public.upa_cargo_manifest.collected_at_utc IS '이 합성 데이터를 생성한 시각';
COMMENT ON COLUMN public.upa_cargo_manifest.quality_flag IS '데이터 품질 플래그';
COMMENT ON COLUMN public.upa_cargo_manifest.is_synthetic IS '합성 데이터 여부 — 전 행 true. 실데이터 아님';
COMMENT ON COLUMN public.upa_cargo_manifest.chem_id IS 'MSDS 물질 ID. gen_cargo_manifest.UN_TO_CHEM_ID 로 생성 시점에 확정. UN번호 조인의 다대일 팬아웃(588->710행)과 원유 UN1267 vs MSDS UN1993 불일치 회피(2026-09-21)';
CREATE TABLE IF NOT EXISTS public.upa_vessel_position (
    callsgn text,
    ptent_yr double precision,
    voyage_no double precision,
    mmsi bigint,
    imo_no double precision,
    vessel_name text,
    longitude double precision,
    latitude double precision,
    sog bigint,
    cog bigint,
    rot double precision,
    heading bigint,
    draught double precision,
    nav_status_code text,
    received_at_utc timestamp with time zone,
    source_system text,
    source_table text,
    collected_at_utc timestamp with time zone,
    quality_flag text,
    is_synthetic boolean,
    voyage_no_filled text,
    port_call_id text,
    record_uid text,
    vessel_uid text,
    vessel_uid_source text
);
COMMENT ON TABLE public.upa_vessel_position IS 'UPA 항내 선박위치(getVslPstnInfo). 현재 주력 실시간 위치 소스(레거시 AIS 대체) — callsgn 네이티브 제공 + 울산 항내 스코프라 신뢰도가 더 높음';
COMMENT ON COLUMN public.upa_vessel_position.callsgn IS '호출부호';
COMMENT ON COLUMN public.upa_vessel_position.ptent_yr IS '입항연도';
COMMENT ON COLUMN public.upa_vessel_position.voyage_no IS '항차번호';
COMMENT ON COLUMN public.upa_vessel_position.mmsi IS '선박 MMSI';
COMMENT ON COLUMN public.upa_vessel_position.imo_no IS '선박 IMO 번호';
COMMENT ON COLUMN public.upa_vessel_position.vessel_name IS '선박명';
COMMENT ON COLUMN public.upa_vessel_position.longitude IS '경도';
COMMENT ON COLUMN public.upa_vessel_position.latitude IS '위도';
COMMENT ON COLUMN public.upa_vessel_position.sog IS '대지속력(knot)';
COMMENT ON COLUMN public.upa_vessel_position.cog IS '대지침로(deg)';
COMMENT ON COLUMN public.upa_vessel_position.rot IS '선회율(Rate Of Turn)';
COMMENT ON COLUMN public.upa_vessel_position.heading IS '선수방위(deg)';
COMMENT ON COLUMN public.upa_vessel_position.draught IS '흘수(m)';
COMMENT ON COLUMN public.upa_vessel_position.nav_status_code IS '항법상태. 숫자 코드가 아니라 한글 텍스트로 옴(예: "항해(동력)", "정박(계류)") — AIS 표준 코드와 다른 체계이니 주의';
COMMENT ON COLUMN public.upa_vessel_position.received_at_utc IS '위치 수신 시각';
COMMENT ON COLUMN public.upa_vessel_position.source_system IS '데이터 출처 시스템명(UPA)';
COMMENT ON COLUMN public.upa_vessel_position.source_table IS '출처 원본 API명(VslPstnInfoService/getVslPstnInfo)';
COMMENT ON COLUMN public.upa_vessel_position.collected_at_utc IS '수집 시각. 설계는 10분 간격 폴링이나 실제 운영 여부는 별도 확인 필요';
COMMENT ON COLUMN public.upa_vessel_position.quality_flag IS '데이터 품질 플래그';
COMMENT ON COLUMN public.upa_vessel_position.is_synthetic IS '합성 데이터 여부';
COMMENT ON COLUMN public.upa_vessel_position.voyage_no_filled IS 'voyage_no 결측 시 대체로 채운 값';
COMMENT ON COLUMN public.upa_vessel_position.port_call_id IS '항차 식별자';
COMMENT ON COLUMN public.upa_vessel_position.record_uid IS '행 고유키';
COMMENT ON COLUMN public.upa_vessel_position.vessel_uid IS '파이프라인 표준 선박 식별자(MMSI-First)';
COMMENT ON COLUMN public.upa_vessel_position.vessel_uid_source IS 'vessel_uid를 만든 근거 필드명';
CREATE TABLE IF NOT EXISTS public.upa_berth_facility (
    "prtagCd" bigint,
    port_name text,
    wharf_name text,
    "fcltCd" text,
    "fcltSubCd" double precision,
    length_m double precision,
    depth_m double precision,
    berth_capacity double precision,
    berth_vessel_count bigint,
    unload_capacity double precision,
    handling_cargo_name text,
    wharf_se_name text,
    latitude double precision,
    longitude double precision,
    port_operator_name text,
    source_system text,
    source_table text,
    collected_at_utc timestamp with time zone,
    quality_flag text,
    is_synthetic boolean,
    record_uid text
);
COMMENT ON TABLE public.upa_berth_facility IS 'UPA 항만시설(부두) 상세 제원. berth_capacity(접안능력)가 69개 선석 중 2개만 실값이 있는 등 결측이 많은 알려진 한계가 있음';
COMMENT ON COLUMN public.upa_berth_facility."prtagCd" IS '항만청 코드(원문 API 필드명 그대로 보존)';
COMMENT ON COLUMN public.upa_berth_facility.port_name IS '항만명';
COMMENT ON COLUMN public.upa_berth_facility.wharf_name IS '부두명(다른 소스와 표기가 다를 수 있음 — facility_name 정규화 필요)';
COMMENT ON COLUMN public.upa_berth_facility."fcltCd" IS '시설코드(원문 API 필드명 그대로 보존)';
COMMENT ON COLUMN public.upa_berth_facility."fcltSubCd" IS '시설 하위코드';
COMMENT ON COLUMN public.upa_berth_facility.length_m IS '안벽 길이(m)';
COMMENT ON COLUMN public.upa_berth_facility.depth_m IS '수심(m)';
COMMENT ON COLUMN public.upa_berth_facility.berth_capacity IS '선석 접안능력(대부분 결측 — 69개 선석 중 2개만 실값 있음, 알려진 한계)';
COMMENT ON COLUMN public.upa_berth_facility.berth_vessel_count IS '동시접안 가능 척수';
COMMENT ON COLUMN public.upa_berth_facility.unload_capacity IS '하역능력';
COMMENT ON COLUMN public.upa_berth_facility.handling_cargo_name IS '취급화물명(원문 API 표기)';
COMMENT ON COLUMN public.upa_berth_facility.wharf_se_name IS '부두 구분명(안벽/돌핀 등 구조 구분)';
COMMENT ON COLUMN public.upa_berth_facility.latitude IS '위도';
COMMENT ON COLUMN public.upa_berth_facility.longitude IS '경도';
COMMENT ON COLUMN public.upa_berth_facility.port_operator_name IS '운영사명';
COMMENT ON COLUMN public.upa_berth_facility.source_system IS '데이터 출처 시스템명(UPA)';
COMMENT ON COLUMN public.upa_berth_facility.source_table IS '출처 원본 API명(getGisBaseHrbrFcltDtlInfo)';
COMMENT ON COLUMN public.upa_berth_facility.collected_at_utc IS '수집 시각';
COMMENT ON COLUMN public.upa_berth_facility.quality_flag IS '데이터 품질 플래그';
COMMENT ON COLUMN public.upa_berth_facility.is_synthetic IS '합성 데이터 여부';
COMMENT ON COLUMN public.upa_berth_facility.record_uid IS '행 고유키(UPSERT용, wharf_name 기준)';
CREATE TABLE IF NOT EXISTS public.upa_port_call (
    callsgn text,
    ptent_vts_yr bigint,
    voyage_no bigint,
    comm_count bigint,
    vessel_name text,
    vessel_name_en text,
    arrival_at_utc timestamp with time zone,
    departure_at_utc timestamp with time zone,
    io_vts_type_code bigint,
    io_vts_name text,
    facility_spec_code text,
    facility_spec_sub_code bigint,
    facility_name text,
    updated_at_utc timestamp with time zone,
    job_at_utc timestamp with time zone,
    source_system text,
    source_table text,
    collected_at_utc timestamp with time zone,
    quality_flag text,
    is_synthetic boolean,
    ptent_yr bigint,
    voyage_no_filled bigint,
    port_call_id text,
    record_uid text
);
COMMENT ON TABLE public.upa_port_call IS 'UPA VTS(선박교통관제) 운항정보(getVtsBaseVslNvgtInfo) 로그. 한 항차(입항)마다 입항/접안/이안/투묘/양묘/이선/출항 이벤트가 각각 별도 행(comm_count)으로 쌓인다. "판정"이 아니라 "이미 일어난 사실의 사후 기록"이라 사전 승인 워크플로의 입력값으로는 못 쓰고, 선박이 실제로 언제 어느 시설(facility_name)에 있었는지의 정본으로만 씀(점유 충돌 방지 보조신호, 사후 감사용)';
COMMENT ON COLUMN public.upa_port_call.callsgn IS '호출부호';
COMMENT ON COLUMN public.upa_port_call.ptent_vts_yr IS '입항연도(VTS 원문 필드명)';
COMMENT ON COLUMN public.upa_port_call.voyage_no IS '항차번호';
COMMENT ON COLUMN public.upa_port_call.comm_count IS '같은 항차 내 이벤트(입항/접안/이안/투묘/양묘/이선/출항) 순번';
COMMENT ON COLUMN public.upa_port_call.vessel_name IS '선박명(국문)';
COMMENT ON COLUMN public.upa_port_call.vessel_name_en IS '선박명(영문)';
COMMENT ON COLUMN public.upa_port_call.arrival_at_utc IS '입항일시. 같은 항차의 모든 이벤트 행에 동일 값이 들어감(항차 단위 값)';
COMMENT ON COLUMN public.upa_port_call.departure_at_utc IS '출항일시. 아직 출항 전이면 NULL(=서류상 재항 중)';
COMMENT ON COLUMN public.upa_port_call.io_vts_type_code IS '입출항관제 이벤트 유형 코드: 3=입항, 4=접안, 5=이안, 6=이선, 7=투묘, 8=양묘, 9=출항';
COMMENT ON COLUMN public.upa_port_call.io_vts_name IS 'io_vts_type_code의 한글명';
COMMENT ON COLUMN public.upa_port_call.facility_spec_code IS '시설코드';
COMMENT ON COLUMN public.upa_port_call.facility_spec_sub_code IS '시설 하위코드';
COMMENT ON COLUMN public.upa_port_call.facility_name IS '이 이벤트가 발생한 시설명(부두/정박지 등). VTS 실측 원문 — 이 프로젝트에서 "선박이 실제 어디 있었는지"의 정본값';
COMMENT ON COLUMN public.upa_port_call.updated_at_utc IS 'UPA 시스템에서 이 기록이 갱신된 시각(=사후 확인 시각, job_at_utc와 비슷하거나 그 이후)';
COMMENT ON COLUMN public.upa_port_call.job_at_utc IS '이 이벤트가 실제로 발생한 시각';
COMMENT ON COLUMN public.upa_port_call.source_system IS '데이터 출처 시스템명(UPA)';
COMMENT ON COLUMN public.upa_port_call.source_table IS '출처 원본 API명(VslNvgtInfoService/getVtsBaseVslNvgtInfo)';
COMMENT ON COLUMN public.upa_port_call.collected_at_utc IS '파이프라인이 이 행을 수집한 시각. 10분 간격 폴링 설계이나 실제로는 수동/간헐 실행됨(알려진 한계)';
COMMENT ON COLUMN public.upa_port_call.quality_flag IS '데이터 품질 플래그';
COMMENT ON COLUMN public.upa_port_call.is_synthetic IS '합성 데이터 여부';
COMMENT ON COLUMN public.upa_port_call.ptent_yr IS 'ptent_vts_yr과 동일 값의 보정 컬럼';
COMMENT ON COLUMN public.upa_port_call.voyage_no_filled IS 'voyage_no 결측 시 대체로 채운 값';
COMMENT ON COLUMN public.upa_port_call.port_call_id IS '항차 식별자(callsgn_ptentyr_voyageno 조합)';
COMMENT ON COLUMN public.upa_port_call.record_uid IS '행 고유키(과거 UPSERT 키였으나 현재는 (port_call_id, comm_count) 자연키 사용)';
CREATE UNIQUE INDEX IF NOT EXISTS upa_anchorage_uidx__facility_code__index_no ON public.upa_anchorage USING btree (facility_code, index_no);
CREATE UNIQUE INDEX IF NOT EXISTS upa_berth_facility_uidx__wharf_name ON public.upa_berth_facility USING btree (wharf_name);
CREATE UNIQUE INDEX IF NOT EXISTS upa_cargo_manifest_uidx__record_uid ON public.upa_cargo_manifest USING btree (record_uid);
CREATE UNIQUE INDEX IF NOT EXISTS upa_port_call_uidx__port_call_id__comm_count ON public.upa_port_call USING btree (port_call_id, comm_count);
CREATE UNIQUE INDEX IF NOT EXISTS upa_vessel_position_uidx__vessel_uid ON public.upa_vessel_position USING btree (vessel_uid);
