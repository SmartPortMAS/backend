-- 0034 되돌리기 — 두 뷰를 0029 정의(필요 여유 = 흘수 10%)로 되돌린다.

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

CREATE OR REPLACE VIEW mart.berth_audit AS
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
