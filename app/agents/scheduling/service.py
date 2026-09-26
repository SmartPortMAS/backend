"""스케줄링 에이전트 — 정해진 선석 하나의 확인(검증모드)과 대체 선석 제안.

  build_candidate_for_wharf_name  배정된 선석이 이 배에 맞는가 — 가용수심(해도 + 체류 중
                                  최저 조위) − 흘수 ≥ 필요 여유(ukc.required_ukc_m), 이웃 화물
  suggest_alternative_berths      맞지 않을 때 관제사에게 보일 대체 선석(제안, 배정 아님)

LLM을 쓰지 않는 결정적 판단이다.

[2026-09-27] 탐색모드(find_berth_candidates — 카테고리 기준 top-3)와 점유 시 대체 선석 →
정박지 대기 재탐색(resolve_berth_assignment)을 걷어냈다(27번 설계안 D단계). 우리는 배정하지
않는다. 정박지는 위치로 관찰만 한다(/dashboard/anchorages).
"""

from datetime import datetime, timezone

from neo4j import AsyncDriver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.msds_context import resolve_cargo
from app.agents.safety.schemas import AdjacentCargo, CargoRef
from app.models import MsdsChemical
from app.services.tide import min_tide_in_window

from .category_map import representative_chem_id
from .ukc import required_ukc_m
from .graph_queries import (
    find_adjacent_categories,
    find_eligible_berths,
    get_berth_by_wharf_name,
    get_chemical_category,
)
from .occupancy import find_overlapping_reservations
from .schemas import (
    BerthCandidate,
    ConflictingPortCall,
    OccupancyStatus,
    VesselSpec,
)



def _cargo_display_name(row: MsdsChemical) -> str:
    return row.name_ko or row.name_en or row.chem_id


_QUERY_REAL_ADJACENT_CARGO = text("""
    -- mart.berth_current_cargo(실제 재항 화물) -> facility_alias -> wharf_name.
    --
    -- facility_alias의 사전(source_names)이 upa_port_call(VTS 원문)과
    -- upa_cargo_manifest(합성 화물의 자체 표기, 예: 'S-Oil 1부두') 둘 다 포함한다
    -- (mart_views.sql 0-B절) — berth_current_cargo.facility_name이
    -- COALESCE(cm.facility_name, ip.facility_name)라 두 어휘가 섞여 나오는데,
    -- 사전이 이제 둘 다 알고 있으므로 단일 조인으로 끝난다(예전엔 여기서
    -- COALESCE 폴백 정규화를 따로 했었는데, 근본 원인을 사전 쪽에서 없앴다).
    SELECT fa.wharf_name, bcc.chem_id, bcc.cas_no
    FROM mart.berth_current_cargo bcc
    JOIN mart.facility_alias fa
      ON fa.source_name = bcc.facility_name AND fa.facility_type = 'BERTH'
    WHERE bcc.chem_id IS NOT NULL
      AND fa.wharf_name = ANY(CAST(:wharf_names AS text[]))
      -- [2026-09-26] 판정 대상 배 자신의 화물은 '인접 화물'이 아니다. 같은 부두의
      -- 선석끼리도 ADJACENT_TO 로 이어져 있어(로더 주석: 실제로 맞닿아 있어 의도한 것)
      -- 이걸 빼지 않으면 자기 화물과 혼재 판정을 했다(실측: D8QR@S-Oil 3부두의 인접
      -- 화물이 자기 화물 2건뿐이었다).
      AND (
          CAST(:exclude_call_sign AS text) IS NULL
          OR upper(btrim(bcc.callsgn)) <> upper(btrim(CAST(:exclude_call_sign AS text)))
      )
""")

# 이웃 부두에 지금 실제로 붙어 있는 배가 있는가 — occupancy.py 와 같은 뷰·같은 기준.
_QUERY_OCCUPIED_WHARFS = text("""
    SELECT DISTINCT vp.berth_name
    FROM mart.vessel_presence vp
    WHERE vp.presence_zone = 'BERTH'
      AND vp.berth_name = ANY(CAST(:wharf_names AS text[]))
      AND (
          CAST(:exclude_call_sign AS text) IS NULL
          OR upper(btrim(vp.callsgn)) <> upper(btrim(CAST(:exclude_call_sign AS text)))
      )
""")


async def _occupied_wharfs(
    db: AsyncSession, wharf_names: list[str], *, exclude_call_sign: str | None = None
) -> set[str]:
    """wharf_names 중 지금 (판정 대상 배를 빼고) 배가 붙어 있는 부두."""
    if not wharf_names:
        return set()
    rows = await db.execute(
        _QUERY_OCCUPIED_WHARFS,
        {"wharf_names": wharf_names, "exclude_call_sign": exclude_call_sign},
    )
    return {r[0] for r in rows}


async def _real_adjacent_cargo_by_wharf(
    db: AsyncSession, wharf_names: list[str], *, exclude_call_sign: str | None = None
) -> dict[str, list[dict]]:
    """mart.berth_current_cargo에서 실제 재항 화물을 wharf_name별로 조회.

    chem_id가 NULL인 행(위험물인데 정체 미확인, UN번호 결측 등)은 CargoRef를
    만들 수 없어 제외한다 — safety/schemas.py의 CargoRef 제약과 동일 이유
    (V-DG-01과 같은 성격의 한계: 식별 불가 화물은 애초에 판정 입력이 안 된다).
    """
    if not wharf_names:
        return {}
    rows = (
        await db.execute(
            _QUERY_REAL_ADJACENT_CARGO,
            {"wharf_names": wharf_names, "exclude_call_sign": exclude_call_sign},
        )
    ).mappings().all()
    grouped: dict[str, list[dict]] = {}
    # 같은 선석에 같은 물질을 실은 배가 여럿이면 뷰에서 행이 여러 개 나온다
    # (berth_current_cargo 는 (callsgn, 물질) 단위). 혼재 판정에는 "그 선석에 그
    # 물질이 있는가"만 중요하므로 물질 단위로 눌러 담는다 — 안 그러면 후보 응답에
    # 같은 화물이 예닐곱 번 반복돼 화면과 LLM 프롬프트가 함께 부풀었다.
    seen_by_wharf: dict[str, set[str]] = {}
    for row in rows:
        wharf = row["wharf_name"]
        seen = seen_by_wharf.setdefault(wharf, set())
        if row["chem_id"] in seen:
            continue
        seen.add(row["chem_id"])
        grouped.setdefault(wharf, []).append(
            {"chem_id": row["chem_id"], "cas_no": row["cas_no"]}
        )
    return grouped


def _adjacent_cargos_for(
    categories_by_neighbor: list[dict],
    real_cargo_by_wharf: dict[str, list[dict]],
    occupied_wharfs: set[str],
) -> list[AdjacentCargo]:
    """인접 선석별 화물을 채운다.

    mart.berth_current_cargo(실제 재항 화물)가 있으면 그걸 쓰고, 없으면(화물
    manifest가 아직 합성 데이터 위주라 커버리지가 낮음·facility_alias 매칭 실패 등)
    그 부두에 배가 실제로 붙어 있을 때만(occupied_wharfs) 카테고리 대표값으로
    근사한다 — category_map.py의 원래 설계를 완전히 버리지 않고 폴백으로 남긴 이유는, "실데이터가 없다"를 "위험이 없다"로
    착각하면 안전 판정을 낙관적으로 왜곡하기 때문이다. 실데이터가 있으면 그게
    항상 우선한다 — 근사값보다 신뢰도가 높다(cargo_msds ★ 안전관제 핵심 뷰 참고).
    """
    adjacent_cargos: list[AdjacentCargo] = []
    for entry in categories_by_neighbor:
        real_cargos = real_cargo_by_wharf.get(entry.get("adjacent_wharf_name") or "", [])
        if real_cargos:
            for rc in real_cargos:
                adjacent_cargos.append(
                    AdjacentCargo(
                        berth_name=entry["adjacent_berth_id"],
                        cargo=CargoRef(chem_id=rc["chem_id"], cas_no=rc["cas_no"]),
                        distance_m=entry.get("distance_m"),
                    )
                )
            continue
        # [2026-09-26] 배가 없는 부두는 폴백하지 않는다. 폴백은 "배는 있는데 화물을
        # 모른다"를 위한 것이지, 빈 부두에 가상 화물을 세우는 용도가 아니다
        # (실측: 온산 이웃 부두 12곳 중 빈 부두 5곳에 대표 화물이 들어가고 있었다).
        if entry.get("adjacent_wharf_name") not in occupied_wharfs:
            continue
        for category in entry["categories"]:
            chem_id = representative_chem_id(category)
            if chem_id is None:
                continue
            adjacent_cargos.append(
                AdjacentCargo(
                    berth_name=entry["adjacent_berth_id"],
                    cargo=CargoRef(chem_id=chem_id),
                    distance_m=entry.get("distance_m"),
                )
            )
    return adjacent_cargos


async def _eligible_for_all_cargos(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    *,
    primary_category: str,
    extra_cargos: list[CargoRef],
    min_depth: float,
    dwt_t: float | None,
) -> tuple[list[dict], str | None, str]:
    """주 화물 카테고리의 적합 선석 중 **추가 화물 카테고리도 모두 취급하는** 선석만.

    [2026-09-25] 한 배가 여러 화물을 싣게 되면서 넣었다. 카테고리를 모르는 추가
    화물이 있으면 걸러낼 근거가 없으므로 이유를 돌려주고 후보를 비운다 — 모르는 걸
    '어디든 된다'로 밀지 않는다.
    """
    eligible = await find_eligible_berths(
        neo4j_driver, category=primary_category, min_depth=min_depth, dwt_t=dwt_t,
    )
    categories = {primary_category}
    for extra in extra_cargos:
        row = await resolve_cargo(db, extra)
        cat = await get_chemical_category(neo4j_driver, row.chem_id)
        if not cat:
            return [], f"함께 실은 화물({row.chem_id})의 선석 카테고리를 알 수 없습니다.", primary_category
        categories.add(cat)
    for cat in categories - {primary_category}:
        allowed = {
            r["berth_id"]
            for r in await find_eligible_berths(neo4j_driver, category=cat, min_depth=min_depth, dwt_t=dwt_t)
        }
        eligible = [r for r in eligible if r["berth_id"] in allowed]
    # 안내 문구용 — 주 화물 카테고리를 앞에, 나머지는 이름순.
    label = "·".join([primary_category, *sorted(categories - {primary_category})])
    return eligible, None, label


# 사전배정 시설명 -> 정본 wharf_name.
#
# [2026-09-21] berth_id 경로 추가.
#   검증모드의 입력은 mart.dashboard_current.facility_name 인데, 그 값은
#   arrival_views_v2.sql 에서
#       COALESCE(m.berth_id, m.wharf_name, pm.arrival_facility_nm)
#   로 만들어진다. 즉 **선석 단위 식별자(berth_id)가 1순위**다.
#   그런데 mart.facility_alias 는 PORT-MIS/VTS **원문 표기**를 wharf_name 에
#   잇는 사전이라 berth_id 형식('3부두-2선석', 'S-Oil부이')은 들어 있지 않다.
#
#   실측(2026-09-21, 검증모드 대상 고유 시설명 11종):
#       별칭 사전으로 해소        7종
#       berth_id 로 해소(추가분)  2종  <- 이 UNION 이 살리는 몫
#       어느 쪽도 아님            2종  ('장생포호안' — 선석 마스터에 없는 호안,
#                                      '신항남방파제T/S부두 01' — 공백 표기 차이)
#   남은 2종은 입력값 그대로 시도하고, 그래도 못 찾으면 NO_ELIGIBLE_BERTH 로
#   떨어진다. 없는 선석을 지어내지 않는다.
_QUERY_RESOLVE_WHARF_ALIAS = text("""
    SELECT wharf_name FROM (
        SELECT wharf_name, 1 AS pri
        FROM mart.facility_alias
        WHERE source_name = :name AND facility_type = 'BERTH'
        UNION ALL
        SELECT wharf_name, 2 AS pri
        FROM berth
        WHERE berth_id = :name
    ) x
    ORDER BY pri
    LIMIT 1
""")


def _tide_window(window_start: datetime, window_end: datetime) -> tuple[datetime, datetime]:
    """조위 판정에 쓸 구간 — 시작을 **지금**으로 당긴다.

    [2026-09-22] `window_start` 는 그 배가 **실제로 접안한 시각**이라 이미 붙어 있는
    배는 과거다(실측: 9/12·9/18 접안). 그런데 `tide_forecast` 는 수집을 시작한 날부터만
    있어서(9/20~) 과거 쪽이 비고, 그대로 두면 `fully_covered` 가 False 가 되어
    **이미 안전하게 정박 중인 배 30척이 한꺼번에 판정불가로 떨어졌다.**

    과거 구간을 판정에서 빼는 것이 맞다. 이 판정이 답하는 질문은 "이 배를 지금 이대로
    두어도 되는가"이고, 그 답을 바꿀 수 있는 것은 **앞으로 올 저조**뿐이다. 사흘 전
    저조는 이미 지나갔고 배는 닿지 않았다 — 되짚어 막을 수 있는 위험이 아니다.

    그래서 이 판정의 뜻은 정확히 "**남은** 체류 구간의 최저 조위"다. 판정 근거 문장에
    나가는 시각도 앞으로의 시각이므로 관제사가 읽는 뜻과 어긋나지 않는다.

    입항 전 판정(구간 시작이 미래)에는 아무 영향이 없다 — `now` 가 시작보다 이르다.
    """
    now = datetime.now(timezone.utc)
    start = max(window_start, now)
    return start, max(window_end, start)


async def build_candidate_for_wharf_name(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    *,
    wharf_name: str,
    vessel: VesselSpec,
    window_start: datetime,
    window_end: datetime,
    draught_margin_m: float | None = None,
) -> tuple[BerthCandidate | None, str | None, bool]:
    """이미 정해진 선석 이름(사전배정 선석) 하나를 검증용 BerthCandidate로 만든다.

    find_berth_candidates(카테고리로 top-3 새로 탐색)와 달리, 특정 선석 하나가
    지금 안전한지만 확인하는 용도다(검증모드) — 화물 카테고리로 거르지 않는다.

    입력 wharf_name은 실시간 위치 조인(mart.berth_current_cargo.facility_name)에서
    올 수 있어 VTS 원문 표기('SK2부두 01')일 수 있다. Neo4j Berth.wharf_name은
    선석 제원 마스터 표기('SK2부두(민유)' 등)라 문자 그대로 다를 수 있으므로,
    mart.facility_alias로 먼저 정규화한다(occupancy.py가 아직 못 하고 있는 것과
    같은 매칭 문제 — 여기서는 새로 만드는 경로라 처음부터 정규화를 거친다).
    별칭 사전에 없으면 입력값을 그대로 시도한다(이미 정본 표기일 수 있으므로).

    Returns:
        (candidate, None, False) — 찾았고 흘수 여유(draught_margin_m) 조건도 만족
        (None, reason, evidence_missing) — 확인 실패.

    세 번째 값이 **근거 부족인지 실제 부적합인지**를 가른다(회의 §4 "근거 부족을
    안전과 구분"). 표기를 못 찾은 것·조위 예보가 없는 것은 근거 부족이라
    '판정불가'로 가야 하고, 가용수심 대비 흘수가 실제로 모자란 것은 '부적합'이다.
    둘을 뭉치면 관제사가 "이 배에 문제가 있다"와 "우리가 모른다"를 구분할 수 없다.
    """
    alias_row = (
        await db.execute(_QUERY_RESOLVE_WHARF_ALIAS, {"name": wharf_name})
    ).mappings().first()
    canonical_wharf_name = alias_row["wharf_name"] if alias_row else wharf_name

    berth = await get_berth_by_wharf_name(neo4j_driver, wharf_name=canonical_wharf_name)
    if berth is None:
        return None, f"선석 '{wharf_name}'을(를) 찾을 수 없습니다(선석 제원 마스터 미등록).", True

    if berth["depth_m"] is None:
        return None, f"선석 '{canonical_wharf_name}'의 수심 정보가 없어 안전 여부를 판단할 수 없습니다.", True

    # ── 흘수 여유 = (해도 수심 + 체류 중 최저 조위) − 흘수 ──────────────────
    #
    # [2026-09-21, D2 ③] 조위 항을 넣었다. 예전엔 해도 수심만 봤다.
    #
    # 조위는 하루 두 번 오르내리므로 **배가 붙어 있는 동안 가장 얕아지는 순간**이
    # 안전을 정한다. 접안 순간만 보면 통과인데 몇 시간 뒤 바닥에 닿는 배를 놓친다.
    # 오경보 백테스트가 실제 사례를 잡았다 — SEA DRAGON/S-Oil 1부두는 접안 시각
    # 기준 +0.26m 였지만 체류 중 최저는 -0.21m 였다(app/services/tide.py 참고).
    #
    # 예보를 쓴다. 판정 대상이 미래 구간이라 관측(tide_obs, 과거만 있음)으로는
    # 잴 수 없다.
    _tide_start, _tide_end = _tide_window(window_start, window_end)
    tide = await min_tide_in_window(db, window_start=_tide_start, window_end=_tide_end)

    if tide is None:
        # ★ 여기서 해도 수심만으로 통과시키지 않는다. 조위를 모르면 가용수심을
        #   모르는 것이고, 모르는 것은 '판정불가'다(회의 §4). 호출부가 이 문자열을
        #   받아 UNKNOWN 으로 기록한다.
        return None, (
            f"선석 '{canonical_wharf_name}'의 체류 구간 조위 예보가 없어 "
            f"가용수심을 계산할 수 없습니다(예보 범위 밖)."
        ), True

    if not tide.fully_covered:
        # [2026-09-22] 예보가 체류 구간을 다 덮지 못한 경우다(구간이 예보 밖으로
        # 걸치거나 중간에 수집 구멍). 이때 tide.level_cm 은 "본 만큼의 최저"라서
        # 실제 최저보다 **높다** — 그대로 쓰면 못 본 구간의 저조를 놓치고 통과시킨다.
        # 접안 순간만 보다가 체류 중 최저를 놓치는 것과 같은 실패라, 같은 규칙으로
        # 판정불가로 돌린다.
        return None, (
            f"선석 '{canonical_wharf_name}'의 조위 예보가 체류 구간을 다 덮지 못해 "
            f"체류 중 최저 조위를 확정할 수 없습니다"
            f"(예보 범위 밖이거나 수집 결측 — data-pipeline tide_forecast 확인 필요)."
        ), True

    tide_m = tide.level_cm / 100.0
    available_depth_m = berth["depth_m"] + tide_m
    actual_margin_m = available_depth_m - vessel.draught_m

    # [2026-09-27] 필요 여유는 ukc.required_ukc_m — 대체안·대시보드 흘수 점검과 같은 규칙.
    if draught_margin_m is None:
        draught_margin_m = required_ukc_m(vessel.draught_m)
    if actual_margin_m < draught_margin_m:
        # [2026-09-22] '선석 확인 요청' 을 먼저 가린다 — mart.berth_draught_check 의
        # CHECK 판정과 같은 규칙이다.
        #
        # 한 부두 안에서도 선석마다 수심이 다르다(실측: 4부두 9~11m · SK2부두 7.5~8m).
        # 위 depth_m 은 그중 가장 얕은 값이고, 우리는 이 배가 몇 번 선석에 붙는지
        # 모른다 — VTS·PORT-MIS 어느 쪽도 선석 번호를 주지 않는다. 가장 깊은
        # 선석이면 기준을 넘는 경우, 시스템이 할 말은 "불가"가 아니라 "어느 선석인지
        # 확인"이다. 항만은 수심이 맞는 선석에 배정하기 때문이다.
        #
        # 근거 부족(True)으로 돌려보내 '판정불가'가 되게 한다. 부적합으로 적으면
        # 실제로는 붙어도 되는 배에 하역보류가 걸린다.
        depth_max_m = berth.get("depth_max_m")
        if depth_max_m is not None and depth_max_m > berth["depth_m"] \
                and (depth_max_m + tide_m - vessel.draught_m) >= draught_margin_m:
            return None, (
                f"선석 '{canonical_wharf_name}'은 선석마다 수심이 달라"
                f"(가장 얕은 곳 {berth['depth_m']}m · 가장 깊은 곳 {depth_max_m}m) "
                f"어느 선석에 접안하는지 확인이 필요합니다 — 가장 깊은 선석이면 "
                f"흘수 {vessel.draught_m}m 에 여유가 있습니다."
            ), True
        # 조위에 편차 보정이 들어갔으면 얼마를 더했는지 문장에 남긴다 — 관제사가
        # 예보 원값과 다른 숫자를 보고 의아해하지 않게(services/tide.py 한계 ②).
        bias_note = (
            f", 실측 보정 {tide.bias_cm:+.1f}cm" if tide.bias_sample_n else ""
        )
        return None, (
            f"선석 '{canonical_wharf_name}' 가용수심 {available_depth_m:.2f}m"
            f"(해도 {berth['depth_m']}m + 체류 중 최저 조위 {tide_m:+.2f}m{bias_note}, "
            f"{tide.at_utc:%m-%d %H:%M}Z) 대비 흘수여유가 {actual_margin_m:.2f}m로 "
            f"요구 기준({draught_margin_m:.2f}m)에 못 미칩니다(선박 흘수 {vessel.draught_m}m)."
        ), False

    # 점유 판정은 berth_assignment(우리 배정 기록)만 본다 — find_berth_candidates와
    # 동일 결정(2026-08-19, 위 주석 참고).
    reservation_map = await find_overlapping_reservations(
        db, berth_ids=[berth["berth_id"]], window_start=window_start, window_end=window_end,
        exclude_call_sign=vessel.call_sign,
    )
    conflicts = reservation_map.get(berth["berth_id"], [])
    status = OccupancyStatus.OCCUPIED if conflicts else OccupancyStatus.AVAILABLE

    adjacency_map = await find_adjacent_categories(neo4j_driver, berth_ids=[berth["berth_id"]])
    adjacent_wharf_names = list({
        entry["adjacent_wharf_name"]
        for entry in adjacency_map.get(berth["berth_id"], [])
        if entry.get("adjacent_wharf_name")
    })
    real_cargo_by_wharf = await _real_adjacent_cargo_by_wharf(
        db, adjacent_wharf_names, exclude_call_sign=vessel.call_sign
    )
    occupied_wharfs = await _occupied_wharfs(
        db, adjacent_wharf_names, exclude_call_sign=vessel.call_sign
    )

    candidate = BerthCandidate(
        rank=1,
        berth_id=berth["berth_id"],
        wharf_name=berth["wharf_name"],
        port_name=berth["port_name"],
        depth_m=berth["depth_m"],
        berth_group=berth.get("berth_group"),
        onsan_scope=bool(berth.get("onsan_scope")),
        draught_margin_m=actual_margin_m,
        occupancy_status=status,
        conflicting_port_calls=[
            ConflictingPortCall(
                vessel_name=c["vessel_name"],
                arrival_at_utc=c["arrival_at_utc"],
                departure_at_utc=c["departure_at_utc"],
            )
            for c in conflicts
        ],
        adjacent_cargos=_adjacent_cargos_for(
            adjacency_map.get(berth["berth_id"], []), real_cargo_by_wharf, occupied_wharfs
        ),
    )
    return candidate, None, False


async def suggest_alternative_berths(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    *,
    cargo: CargoRef,
    vessel: VesselSpec,
    window_start: datetime,
    window_end: datetime,
    exclude_wharf_name: str | None = None,
    draught_margin_m: float | None = None,
    limit: int = 3,
    extra_cargos: list[CargoRef] | None = None,
) -> tuple[list[BerthCandidate], str | None]:
    """배정된 시설이 부적합할 때 내놓을 대체 선석 후보.

    Returns:
        (후보 목록, 못 찾은 이유). 후보가 있으면 이유는 None.

    수심 조건은 **조위를 반영한 소요 해도수심**으로 건다 —
        필요한 해도수심 = 흘수 + 안전여유 − 체류 중 최저 조위
    검증에서 조위를 쓰면서 추천에서 안 쓰면, 통과 기준이 서로 달라
    "지금 자리는 부적합인데 추천된 자리도 사실은 부적합"인 일이 생긴다.
    """
    target_row = await resolve_cargo(db, cargo)
    category = await get_chemical_category(neo4j_driver, target_row.chem_id)
    if not category:
        return [], f"화물({target_row.chem_id})의 선석 카테고리를 알 수 없어 대체안을 찾을 수 없습니다."

    _tide_start, _tide_end = _tide_window(window_start, window_end)
    tide = await min_tide_in_window(db, window_start=_tide_start, window_end=_tide_end)
    if tide is None:
        return [], "체류 구간 조위 예보가 없어 대체안의 가용수심을 계산할 수 없습니다."
    if not tide.fully_covered:
        # 대체안은 "여기라면 안전하다"는 제안이다. 못 본 구간이 있는 조위로 제안하면
        # 지금 자리보다 나을 보장이 없는 자리를 권하게 된다(위 min_tide_in_window 주석).
        return [], (
            "조위 예보가 체류 구간을 다 덮지 못해 대체안의 가용수심을 확정할 수 "
            "없습니다(예보 범위 밖이거나 수집 결측)."
        )
    tide_m = tide.level_cm / 100.0

    if draught_margin_m is None:
        draught_margin_m = required_ukc_m(vessel.draught_m)   # 검증과 같은 규칙(ukc.py)
    min_depth = vessel.draught_m + draught_margin_m - tide_m
    eligible, extra_reason, category_label = await _eligible_for_all_cargos(
        db, neo4j_driver, primary_category=category, extra_cargos=list(extra_cargos or []),
        min_depth=min_depth, dwt_t=vessel.dwt_t,
    )
    if extra_reason:
        return [], extra_reason

    # 같은 계선시설이 Berth 노드로 여러 개 있다(실측: 'S-Oil 2부두' 3개, 'S-Oil 4부두' 2개).
    # 마스터에 선석번호 구분 없이 중복 적재된 것이라, 그대로 두면 **같은 부두가 대체안
    # 1·2·3위를 모두 차지해** 관제사에게 선택지가 하나도 없는 목록이 나간다.
    # 여기서 계선시설 이름 단위로 누른다 — 수심이 가장 깊은 노드를 대표로 쓴다.
    excluded = (exclude_wharf_name or "").strip()
    by_wharf: dict[str, dict] = {}
    for row in eligible:
        name = (row.get("wharf_name") or "").strip()
        if not name or name == excluded:
            continue
        kept = by_wharf.get(name)
        if kept is None or (row.get("depth_m") or 0) > (kept.get("depth_m") or 0):
            by_wharf[name] = row
    eligible = list(by_wharf.values())

    if not eligible:
        return [], (
            f"'{category_label}'를 모두 취급하면서 가용수심 조건(해도 {min_depth:.2f}m 이상)을 "
            f"만족하는 다른 선석이 없습니다."
        )

    berth_ids = [row["berth_id"] for row in eligible]
    # 점유는 **탈락 사유가 아니라 표시 항목**이다(백테스트 S2 — 정보로 강등).
    # 관제사가 "빈 자리부터 보자"고 판단할 수 있게 정렬에만 쓴다.
    live_map = await find_overlapping_reservations(
        db, berth_ids=berth_ids, window_start=window_start, window_end=window_end,
        exclude_call_sign=vessel.call_sign,
    )
    adjacency_map = await find_adjacent_categories(neo4j_driver, berth_ids=berth_ids)
    adjacent_wharf_names = list({
        entry["adjacent_wharf_name"]
        for entries in adjacency_map.values()
        for entry in entries
        if entry.get("adjacent_wharf_name")
    })
    real_cargo_by_wharf = await _real_adjacent_cargo_by_wharf(
        db, adjacent_wharf_names, exclude_call_sign=vessel.call_sign
    )
    occupied_wharfs = await _occupied_wharfs(
        db, adjacent_wharf_names, exclude_call_sign=vessel.call_sign
    )

    candidates: list[BerthCandidate] = []
    for row in eligible:
        occupants = live_map.get(row["berth_id"], [])
        candidates.append(
            BerthCandidate(
                rank=0,
                berth_id=row["berth_id"],
                wharf_name=row["wharf_name"],
                port_name=row["port_name"],
                depth_m=row["depth_m"],
                berth_group=row.get("berth_group"),
                onsan_scope=bool(row.get("onsan_scope")),
                # 가용수심 기준 여유. 해도 수심이 아니라 조위를 더한 값으로 잰다.
                draught_margin_m=row["depth_m"] + tide_m - vessel.draught_m,
                occupancy_status=(
                    OccupancyStatus.OCCUPIED if occupants else OccupancyStatus.AVAILABLE
                ),
                conflicting_port_calls=[
                    ConflictingPortCall(
                        vessel_name=c["vessel_name"],
                        arrival_at_utc=c["arrival_at_utc"],
                        departure_at_utc=c["departure_at_utc"],
                    )
                    for c in occupants
                ],
                adjacent_cargos=_adjacent_cargos_for(
                    adjacency_map.get(row["berth_id"], []), real_cargo_by_wharf, occupied_wharfs
                ),
            )
        )

    # 빈 자리 먼저, 그 다음 여유가 큰 순. 점유는 탈락이 아니라 후순위일 뿐이다.
    candidates.sort(
        key=lambda c: (c.occupancy_status is OccupancyStatus.OCCUPIED, -c.draught_margin_m)
    )
    for i, c in enumerate(candidates[:limit], start=1):
        c.rank = i
    return candidates[:limit], None
