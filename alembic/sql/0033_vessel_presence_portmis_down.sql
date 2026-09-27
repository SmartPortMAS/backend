-- 0033 되돌리기 — mart.vessel_presence 를 0029 정의로 되돌린다(0029_mart_views.sql 786~960행 그대로).
-- 출력 칼럼이 같아 이 뷰에 기대는 뷰(berth_current_cargo · berth_draught_check)는 다시 만들 필요가 없다.

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
