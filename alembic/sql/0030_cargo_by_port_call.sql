-- 0030 — 화물을 콜사인이 아니라 **입항 건**으로 잇는다
--
-- 왜: data-pipeline gen_cargo_manifest v4 가 화물을 PORT-MIS 입항 건
--     (callsgn, entry_year, entry_count) 단위로 만든다. 소비 뷰가 콜사인으로만
--     조인하면 30일 안에 다시 들어온 배(액체선 약 100척, 2026-09-25 로컬 DB)의
--     지난 항차 화물이 이번 항차에 섞인다.
--
-- 무엇을:
--   1. mart.cargo_msds         — 맨 끝에 entry_year · entry_count · port_call_key 추가
--                                (CREATE OR REPLACE 는 뒤에 붙이는 것만 허용 → 기존 소비자 무영향)
--   2. mart.vessel_current_call — 배(콜사인)마다 "지금의 입항 건" 1개
--   3. berth_current_cargo · dashboard_current · approval_candidates — 입항 건으로 조인
--
-- vessel_current_call 규칙 (정확도는 2026-09-25 로컬 DB 에서 채점: 위치 행 중
-- 입항횟수가 있는 199건을 정답으로 두고 그 값을 가린 채 규칙만 적용):
--   (a) 위치 데이터(upa_vessel_position)에 입항연도·횟수가 있으면 그대로 쓴다.
--   (b) 없으면 PORT-MIS 입항 건 중 입항시각 <= now()+12h 인 가장 최근 건.
--       출항 신고는 보지 않는다 — PORT-MIS 는 조회 창이 입항일 기준이라 지난 입항 건의
--       출항 기록이 갱신되지 않는 경우가 많다(7일 넘은 입항 건의 53% 가 출항 NULL).
--       채점: 정답 162 / 오답 25 / 미연결 12.
--   (c) 안전장치: (b)로 고른 건보다 48시간 넘게 늦은 VTS '입항' 기록이 있으면
--       배가 그 뒤에 새로 들어온 것이다 → 연결하지 않는다(화물 없음 = 판정불가).
--       채점: 정답 162 유지 / 오답 25 → 14. 차단 11건은 전부 원래 오답이었다.
--       남는 오답 14건은 PORT-MIS 에 그 입항 건이 없고 VTS 에도 새 기록이 없는 경우다.
--   연결 못 한 배는 이 뷰에 행이 없다 → 화물 없음(판정불가). 2026-09-25 결정.

CREATE OR REPLACE VIEW mart.cargo_msds AS
SELECT
    upper(trim(cm.callsgn))        AS callsgn,
    cm.bl_no,
    cm.facility_name,
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
    cm.is_synthetic,
    cm.cargo_basis,
    -- [0030] 입항 건 키. 적하목록의 ptent_yr/voyage_no 는 text 라 숫자만 남겨 맞춘다.
    k.entry_year,
    k.entry_count,
    CASE WHEN k.entry_year IS NOT NULL AND k.entry_count IS NOT NULL
         THEN upper(trim(cm.callsgn)) || '_' || k.entry_year::text || '_' || lpad(k.entry_count::text, 3, '0')
    END                            AS port_call_key
FROM upa_cargo_manifest cm
CROSS JOIN LATERAL (
    SELECT nullif(regexp_replace(coalesce(cm.ptent_yr, ''), '\D', '', 'g'), '')::int  AS entry_year,
           nullif(regexp_replace(coalesce(cm.voyage_no, ''), '\D', '', 'g'), '')::int AS entry_count
) k
LEFT JOIN mart.msds_flat ms ON ms.chem_id::text = NULLIF(btrim(cm.chem_id::text), '');

CREATE OR REPLACE VIEW mart.vessel_current_call AS
WITH pos AS (
    SELECT DISTINCT ON (upper(btrim(callsgn)))
           upper(btrim(callsgn))  AS callsgn,
           ptent_yr::int          AS entry_year,
           voyage_no::int         AS entry_count
    FROM upa_vessel_position
    WHERE nullif(btrim(callsgn), '') IS NOT NULL
    ORDER BY upper(btrim(callsgn)), received_at_utc DESC NULLS LAST
),
vessels AS (
    SELECT callsgn FROM pos
    UNION
    SELECT DISTINCT upper(btrim(callsgn)) FROM portmis_vessel
    WHERE nullif(btrim(callsgn), '') IS NOT NULL
),
pm_latest AS (
    SELECT v.callsgn, x.entry_year, x.entry_count, x.arrival_at_utc
    FROM vessels v
    JOIN LATERAL (
        SELECT p.entry_year, p.entry_count, p.arrival_at_utc
        FROM portmis_vessel p
        WHERE upper(btrim(p.callsgn)) = v.callsgn
          AND p.arrival_at_utc <= now() + interval '12 hours'
          AND p.entry_year IS NOT NULL AND p.entry_count IS NOT NULL
        ORDER BY p.arrival_at_utc DESC
        LIMIT 1
    ) x ON true
),
vts_in AS (
    SELECT upper(btrim(callsgn)) AS callsgn, max(arrival_at_utc) AS last_in
    FROM upa_port_call
    WHERE io_vts_name = '입항'
    GROUP BY 1
),
resolved AS (
    SELECT v.callsgn,
           CASE WHEN pos.entry_count IS NOT NULL THEN pos.entry_year  ELSE pl.entry_year  END AS entry_year,
           CASE WHEN pos.entry_count IS NOT NULL THEN pos.entry_count ELSE pl.entry_count END AS entry_count,
           CASE WHEN pos.entry_count IS NOT NULL THEN 'AIS 입항횟수' ELSE 'PORT-MIS 최근 입항' END AS basis,
           pl.arrival_at_utc AS portmis_arrival_at_utc
    FROM vessels v
    LEFT JOIN pos       ON pos.callsgn = v.callsgn AND pos.entry_count IS NOT NULL AND pos.entry_year IS NOT NULL
    LEFT JOIN pm_latest pl ON pl.callsgn = v.callsgn
    LEFT JOIN vts_in    vi ON vi.callsgn = v.callsgn
    WHERE pos.entry_count IS NOT NULL
       OR (pl.entry_count IS NOT NULL
           AND NOT coalesce(vi.last_in > pl.arrival_at_utc + interval '48 hours', false))
)
SELECT callsgn, entry_year, entry_count,
       callsgn || '_' || entry_year::text || '_' || lpad(entry_count::text, 3, '0') AS port_call_key,
       basis, portmis_arrival_at_utc
FROM resolved;

COMMENT ON VIEW mart.vessel_current_call IS
    '배(콜사인)마다 지금의 입항 건 1개. 위치 데이터의 입항횟수 > PORT-MIS 최근 입항(48h VTS 안전장치). 연결 못 한 배는 행 없음 = 화물 없음. 0030';

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
-- [0030] 콜사인 조인은 지난 항차 화물까지 붙였다(30일 안 재입항 액체선 약 100척).
JOIN mart.vessel_current_call vc ON vc.callsgn = ip.callsgn
JOIN mart.cargo_msds cm ON cm.port_call_key = vc.port_call_key
WHERE cm.dg_un_no IS NOT NULL;   -- 위험물 화물만 (혼재금지 판정 대상)



CREATE OR REPLACE VIEW mart.dashboard_current AS
WITH msds_by_vessel AS (
    -- [0030] 콜사인이 아니라 그 배의 **현재 입항 건** 화물만 모은다.
    SELECT vc.callsgn,
           min(flash_point_celsius)                    AS min_flash_point_c,
           string_agg(DISTINCT imdg_class, ',')        AS imdg_classes,
           string_agg(DISTINCT signal_word, ',')       AS ghs_signal_words,
           count(*) FILTER (WHERE msds_matched)        AS msds_matched_count
    FROM mart.cargo_msds cm
    JOIN mart.vessel_current_call vc ON vc.port_call_key = cm.port_call_key
    GROUP BY vc.callsgn
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

CREATE OR REPLACE VIEW mart.approval_candidates AS
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
-- [0030] 이 PORT-MIS 입항 건 자신의 화물만 (콜사인 조인은 지난 항차까지 붙였다)
LEFT JOIN mart.cargo_msds cm
       ON cm.port_call_key = upper(btrim(pm.callsgn)) || '_' || pm.entry_year::text
                             || '_' || lpad(pm.entry_count::text, 3, '0')
WHERE pm.port_agency_cd = '820'
  AND pm.is_liquid_cargo_vessel
  AND pm.departure_at_utc IS NULL
  AND (pm.arrival_facility_nm IS NULL OR pm.arrival_facility_nm LIKE '%정박지%')
  AND pm.arrival_at_utc >= (
        (date_trunc('day', (now() AT TIME ZONE 'Asia/Seoul')) + interval '1 day')
        AT TIME ZONE 'Asia/Seoul');
