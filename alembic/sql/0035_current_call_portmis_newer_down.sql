-- 0035 되돌리기 — mart.vessel_current_call 을 0030 정의(AIS 입항횟수 우선)로 되돌린다.

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
