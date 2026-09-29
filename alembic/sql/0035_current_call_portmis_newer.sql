-- 0035 — mart.vessel_current_call: AIS 입항횟수보다 PORT-MIS 입항 건이 새로우면 PORT-MIS 를 따른다.
--
-- 왜: 0030 규칙 (a)는 위치 데이터(upa_vessel_position)의 입항연도·횟수를 무조건 앞세웠다. 그런데
--   이 값은 AIS 정적 정보라 며칠씩 갱신되지 않는다. 실측(2026-09-29, 로컬): 다인3호(212803)는
--   PORT-MIS 새 입항 건 #21 이 오늘 13:00 SK2부두 입항인데 마지막 위치 수신(9/27 23:50)의 #20 이
--   남아, 판정 잡이 #20 의 화물 6종(가솔린·러버 솔벤트 등)으로 판정했다 — 실제 #21 화물은 3종.
--   블루오션·케이티에스 골드·우황도 같은 식으로 한 건 뒤처져 있었다(입항 판정 화면 78척 중 4척).
-- 규칙: 두 값이 다 있으면 (입항연도, 입항횟수)가 큰 쪽. PORT-MIS 는 입항 <= now()+12h 건만 보므로
--   먼 미래 입항 예정 건으로 넘어가지 않는다. 한쪽만 있으면 종전과 같다.
-- 되돌리기: 0035_current_call_portmis_newer_down.sql (0030 정의).

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
           CASE WHEN use_pos THEN pos.entry_year  ELSE pl.entry_year  END AS entry_year,
           CASE WHEN use_pos THEN pos.entry_count ELSE pl.entry_count END AS entry_count,
           CASE WHEN use_pos THEN 'AIS 입항횟수'
                WHEN pos.entry_count IS NOT NULL THEN 'PORT-MIS 최근 입항(AIS 입항횟수보다 새 건)'
                ELSE 'PORT-MIS 최근 입항' END AS basis,
           pl.arrival_at_utc AS portmis_arrival_at_utc
    FROM vessels v
    LEFT JOIN pos       ON pos.callsgn = v.callsgn AND pos.entry_count IS NOT NULL AND pos.entry_year IS NOT NULL
    LEFT JOIN pm_latest pl ON pl.callsgn = v.callsgn
    LEFT JOIN vts_in    vi ON vi.callsgn = v.callsgn
    -- [0035] AIS 입항횟수를 쓰되, PORT-MIS 최근 입항 건(입항 <= now()+12h)이 그보다 새 건이면
    --   PORT-MIS 를 따른다. AIS 정적 정보(입항연도·횟수)는 며칠씩 갱신되지 않는다.
    CROSS JOIN LATERAL (
        SELECT pos.entry_count IS NOT NULL
               AND NOT coalesce((pl.entry_year, pl.entry_count) > (pos.entry_year, pos.entry_count), false)
               AS use_pos
    ) u
    WHERE pos.entry_count IS NOT NULL
       OR (pl.entry_count IS NOT NULL
           AND NOT coalesce(vi.last_in > pl.arrival_at_utc + interval '48 hours', false))
)
SELECT callsgn, entry_year, entry_count,
       callsgn || '_' || entry_year::text || '_' || lpad(entry_count::text, 3, '0') AS port_call_key,
       basis, portmis_arrival_at_utc
FROM resolved;

COMMENT ON VIEW mart.vessel_current_call IS
    '배(콜사인)마다 지금의 입항 건 1개. 위치 데이터의 입항횟수 > PORT-MIS 최근 입항(48h VTS 안전장치). 단 PORT-MIS 최근 입항 건이 AIS 입항횟수보다 새 건이면 PORT-MIS. 연결 못 한 배는 행 없음 = 화물 없음. 0030·0035';
