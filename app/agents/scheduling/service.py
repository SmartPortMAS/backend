"""스케줄링 에이전트 오케스트레이션.

흐름: 화물 조회 -> cargo_category 확인 -> Neo4j로 수심/화물적합 선석 후보 탐색
-> Postgres로 요청 시간대 점유 여부 확인 -> 인접 선석 취급 카테고리로
adjacent_cargos 근사 채움 -> 여유 우선·수심여유 큰 순 정렬 -> 상위 3개 반환.

LLM을 쓰지 않는다 — 계획서상 스케줄링 에이전트는 Neo4j Cypher 쿼리 기반의
결정적 판단이고(안전관제 에이전트만 MSDS 근거 문장 생성에 LLM을 씀), 최종
자연어 종합 판단은 상위 오케스트레이터(이번 범위 밖)의 몫이다.

온산 MVP(feature/onsan-mvp) 이식: resolve_berth_assignment()가 "전용 -> 같은
운영사/파이프라인 대체(SUBSTITUTABLE_WITH) -> 톤수 맞는 정박지 대기
(FALLBACK_ANCHORAGE)" 3단계 배정을 담당한다. find_berth_candidates()가 만드는
"카테고리 적합 상위 3순위" 흐름과는 별개로, 오케스트레이터가 특정 후보(대개
1순위)가 점유 중일 때 호출하는 보조 경로다.
"""

import math
from datetime import datetime, timezone

from neo4j import AsyncDriver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.safety.msds_context import resolve_cargo
from app.agents.safety.schemas import AdjacentCargo, CargoRef
from app.core.exceptions import CargoCategoryUnknownError
from app.models import MsdsChemical
from app.services.tide import min_tide_in_window

from .category_map import representative_chem_id
from .graph_queries import (
    VLCC_BUOY_DWT,
    find_adjacent_categories,
    find_anchorage_candidates,
    find_eligible_berths,
    find_substitutable_berths,
    get_berth_by_wharf_name,
    get_chemical_category,
    select_anchorage_for_dwt,
)
from .occupancy import find_overlapping_reservations
from .schemas import (
    AnchorageAssignment,
    BerthCandidate,
    BerthResolution,
    ConflictingPortCall,
    OccupancyStatus,
    SchedulingRequest,
    SchedulingResult,
    VesselSpec,
)

MAX_CANDIDATES = 3


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
""")


async def _real_adjacent_cargo_by_wharf(
    db: AsyncSession, wharf_names: list[str]
) -> dict[str, list[dict]]:
    """mart.berth_current_cargo에서 실제 재항 화물을 wharf_name별로 조회.

    chem_id가 NULL인 행(위험물인데 정체 미확인, UN번호 결측 등)은 CargoRef를
    만들 수 없어 제외한다 — safety/schemas.py의 CargoRef 제약과 동일 이유
    (V-DG-01과 같은 성격의 한계: 식별 불가 화물은 애초에 판정 입력이 안 된다).
    """
    if not wharf_names:
        return {}
    rows = (
        await db.execute(_QUERY_REAL_ADJACENT_CARGO, {"wharf_names": wharf_names})
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
    categories_by_neighbor: list[dict], real_cargo_by_wharf: dict[str, list[dict]]
) -> list[AdjacentCargo]:
    """인접 선석별 화물을 채운다.

    mart.berth_current_cargo(실제 재항 화물)가 있으면 그걸 쓰고, 없으면(화물
    manifest가 아직 합성 데이터 위주라 커버리지가 낮음·재항 선박 없음·facility_alias
    매칭 실패 등) 카테고리 대표값으로 근사한다 — category_map.py의 원래 설계를
    완전히 버리지 않고 폴백으로 남긴 이유는, "실데이터가 없다"를 "위험이 없다"로
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


async def find_berth_candidates(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    request: SchedulingRequest,
) -> SchedulingResult:
    target_row = await resolve_cargo(db, request.cargo)

    category = await get_chemical_category(neo4j_driver, target_row.chem_id)
    if not category:
        raise CargoCategoryUnknownError(target_row.chem_id)

    min_depth = request.vessel.draught_m + request.draught_margin_m
    eligible = await find_eligible_berths(
        neo4j_driver, category=category, min_depth=min_depth, dwt_t=request.vessel.dwt_t,
    )

    if not eligible:
        return SchedulingResult(
            target_cargo_name=_cargo_display_name(target_row),
            cargo_category=category,
            candidates=[],
            total_eligible_count=0,
        )

    berth_ids = [row["berth_id"] for row in eligible]

    # [2026-09-22] 이 주석은 "점유 판정은 우리 배정 기록(berth_assignment)만 본다 —
    # 이 에이전트가 선석을 직접 배정하는 주체이므로"라고 적혀 있었다. 둘 다 더는
    # 사실이 아니다. 우리는 배정하지 않고(방향 C), berth_assignment 표는 alembic
    # 0027 이 지웠다.
    #
    # 지금 점유는 **실측 위치**로 본다(mart.vessel_presence, occupancy.py).
    # VTS 사후 이력(upa_port_call)을 안 쓴다는 2026-08-19 결정은 그대로 유효하다 —
    # 출항 미기록 유령 재항이 3,910건(최고 8개월분) 있었다. 그건 사후 이력의 문제고,
    # 실시간 위치에는 그 문제가 구조적으로 없다.
    #
    # 점유는 탈락 사유가 아니라 표시 항목이다(백테스트 S2 — 정보로 강등).
    reservation_map = await find_overlapping_reservations(
        db, berth_ids=berth_ids, window_start=request.window_start,
        window_end=request.window_end, exclude_call_sign=request.vessel.call_sign,
    )
    adjacency_map = await find_adjacent_categories(neo4j_driver, berth_ids=berth_ids)
    adjacent_wharf_names = list({
        entry["adjacent_wharf_name"]
        for entries in adjacency_map.values()
        for entry in entries
        if entry.get("adjacent_wharf_name")
    })
    real_cargo_by_wharf = await _real_adjacent_cargo_by_wharf(db, adjacent_wharf_names)

    candidates: list[BerthCandidate] = []
    for row in eligible:
        conflicts = reservation_map.get(row["berth_id"], [])
        status = OccupancyStatus.OCCUPIED if conflicts else OccupancyStatus.AVAILABLE
        candidates.append(
            BerthCandidate(
                rank=0,  # 정렬 후 채움
                berth_id=row["berth_id"],
                wharf_name=row["wharf_name"],
                port_name=row["port_name"],
                depth_m=row["depth_m"],
                berth_group=row.get("berth_group"),
                onsan_scope=bool(row.get("onsan_scope")),
                draught_margin_m=row["depth_m"] - request.vessel.draught_m,
                occupancy_status=status,
                latitude=row.get("latitude"),
                longitude=row.get("longitude"),
                unload_capacity=row.get("unload_capacity"),
                conflicting_port_calls=[
                    ConflictingPortCall(
                        vessel_name=c["vessel_name"],
                        arrival_at_utc=c["arrival_at_utc"],
                        departure_at_utc=c["departure_at_utc"],
                    )
                    for c in conflicts
                ],
                adjacent_cargos=_adjacent_cargos_for(
                    adjacency_map.get(row["berth_id"], []), real_cargo_by_wharf
                ),
            )
        )

    # 여유 선석 우선 → 수심 여유가 작은(딱 맞는) 순 → unload_capacity 소프트 타이브레이크.
    # (08_스케줄링_전면재설계_자동배정_설계문서.md §4.1.3-A, 2026-08-19)
    #
    # "온산 스코프 우선"을 정렬 키로 두지 않는다 — [2026-08-21] find_eligible_berths가
    # graph_queries._CYPHER_FIND_ELIGIBLE_BERTHS에서 onsan_scope를 이미 하드 필터로
    # 적용하므로(대시보드 지도와 배정 범위를 온산항으로 일치시키기 위함) 여기 도달하는
    # 후보는 전부 onsan_scope=true다 — 정렬 기준으로 삼을 변별력이 없다. 값 자체는
    # BerthCandidate에 참고용으로 계속 실어 보낸다.
    #
    # 두 번째 키는 "여유가 큰 순"이 아니라 "잘 맞는 순"이다(best fit) — 예전엔
    # -draught_margin_m이라 여유가 가장 큰 선석이 1순위였는데, 그 결과 흘수 6m짜리
    # 제품유 운반선에게 수심 27m 원유부이가 1순위로 나왔다(2026-08-18 실측). 안전
    # 하한은 이미 위 필터(depth >= draught + margin)가 보장하므로, 남은 여유는
    # 작을수록 좋다 — 깊은 선석은 깊은 배를 위해 비워 둔다.
    #
    # 세 번째 키(unload_capacity)는 §5.2.1-B 소프트 가중치 — 값이 있는 쪽을 약하게
    # 우선하고, 값이 있으면 큰 쪽을 우선한다. 결측(다수)은 "부적합"이 아니라
    # "정보 없음"으로 취급해 순위에서만 밀리고 후보에서 빠지지 않는다.
    candidates.sort(
        key=lambda c: (
            c.occupancy_status is OccupancyStatus.OCCUPIED,
            c.draught_margin_m,
            c.unload_capacity is None,
            -(c.unload_capacity or 0),
        )
    )

    top_candidates = candidates[:MAX_CANDIDATES]
    for rank, candidate in enumerate(top_candidates, start=1):
        candidate.rank = rank

    return SchedulingResult(
        target_cargo_name=_cargo_display_name(target_row),
        cargo_category=category,
        candidates=top_candidates,
        total_eligible_count=len(eligible),
    )


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
    draught_margin_m: float = 1.0,
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

    # find_berth_candidates(탐색모드)의 min_depth = draught + draught_margin_m와
    # 같은 기준. 검증모드라고 더 느슨하게 볼 이유가 없다 — 같은 배가 같은
    # 안전여유 기준을 통과해야 한다.
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
            f"요구 기준({draught_margin_m}m)에 못 미칩니다(선박 흘수 {vessel.draught_m}m)."
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
    real_cargo_by_wharf = await _real_adjacent_cargo_by_wharf(db, adjacent_wharf_names)

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
            adjacency_map.get(berth["berth_id"], []), real_cargo_by_wharf
        ),
    )
    return candidate, None, False


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 좌표 사이 거리(m), WGS84 하버사인. data-pipeline berth_neo4j_loader.py의
    haversine_m과 동일 공식 — 서비스가 분리돼 있어 import 대신 같은 공식을 복제한다."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _sort_substitutes_by_distance(candidate: BerthCandidate, substitutes: list[dict]) -> list[dict]:
    """대체 후보를 candidate(1순위 선석) 좌표 기준 거리 오름차순으로 정렬한다
    (§5.2.1-C, 2026-08-19). candidate나 개별 substitute에 좌표가 없으면(부이 계열
    등, MISSING_COORDINATE) 그 항목은 거리를 알 수 없으므로 목록 뒤쪽으로 보내되
    배정 자체를 막지는 않는다 — "거리 우선순위"만 모를 뿐 적합성은 그대로 유효.
    """
    if candidate.latitude is None or candidate.longitude is None:
        return substitutes  # 기준점 자체가 없음 — 원래 순서(Neo4j 반환 순) 그대로 폴백

    def _distance(sub: dict) -> float:
        lat, lon = sub.get("latitude"), sub.get("longitude")
        if lat is None or lon is None:
            return float("inf")
        return _haversine_m(candidate.latitude, candidate.longitude, lat, lon)

    return sorted(substitutes, key=_distance)


async def resolve_berth_assignment(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    *,
    candidate: BerthCandidate,
    vessel: VesselSpec,
    window_start: datetime,
    window_end: datetime,
    category: str | None = None,
) -> BerthResolution:
    """전용 선석(candidate)이 점유 중일 때 대체 -> 정박지 3단계로 재배정을 시도한다.

    category(선택): 탐색모드(find_berth_candidates)에서 확정된 화물 카테고리.
    주어지면 대체 후보의 SUBSTITUTABLE_WITH.shared_products에 이 카테고리가
    포함된 경우만 배정한다 — 없으면(검증모드, build_candidate_for_wharf_name은
    카테고리를 계산하지 않는다) 이 게이트를 건너뛴다(기존 동작 유지).
    [2026-08-21] 대체 후보는 depth_m/DWT/점유만 확인하고 화물 적합성은 확인하지
    않았다 — SUBSTITUTABLE_WITH 자체가 "같은 운영사 + 카테고리 겹침"으로 미리
    계산되긴 하지만(berth_neo4j_loader.compute_substitutability_pairs) 그건
    두 선석의 전체 취급화물 교집합일 뿐, 지금 배정하려는 화물이 그 교집합
    안에 있다는 보장이 아니다(실측: 유류 전용 달포부두가 점유 중이라 잡화·목재
    부두 용연부두로 디젤이 대체 배정됨 — shared_products="잡화"였는데 그걸
    검증 없이 그대로 받아들였다). onsan_scope 하드필터(위 쿼리 수정)로 이
    특정 사례는 막히지만, 카테고리 자체를 확인하지 않는 구조적 gap은 남아있어
    별도로 막는다.

    온산 MVP(feature/onsan-mvp) 이식: 액체화물 부두는 파이프라인이 특정 탱크단지로
    고정 연결된 전용부두라, 대체는 같은 운영사(SUBSTITUTABLE_WITH) 안에서만 가능하고
    그마저 없으면 실제 선박 DWT에 맞는 정박지 대기가 현실이다(select_anchorage_for_dwt
    — onsan_mvp/scripts/build_substitutability.py, build_anchorage_assignment.py의
    assign_anchorage(dwt=...)와 동일 로직).

    candidate가 이미 여유(AVAILABLE) 상태면 전용 선석을 그대로 확정한다 — 이
    함수는 "점유 중일 때만" 재탐색이 의미가 있다.
    """
    if candidate.occupancy_status is not OccupancyStatus.OCCUPIED:
        # 점유 중일 때(아래 대체/정박지 분기)는 흘수·수심 숫자를 근거로 보여주는데
        # 이 "그대로 확정" 분기만 "사용 가능" 한 줄뿐이었다 — 관제사가 왜 이
        # 선석이 맞는지 확인할 근거가 없었다(2026-08-20 지적).
        return BerthResolution(
            path="전용",
            berth=candidate,
            trace=[
                f"전용 선석 '{candidate.wharf_name}' 사용 가능 "
                f"(수심 {candidate.depth_m}m, 흘수 {vessel.draught_m}m, 여유 {candidate.draught_margin_m:.1f}m)"
            ],
        )

    trace = [f"전용 선석 '{candidate.wharf_name}' 점유 중 (우리 시스템 배정 기록 있음)"]
    substitutes = await find_substitutable_berths(neo4j_driver, berth_id=candidate.berth_id)
    # §5.2.1-C: "미리 정해둔 정렬표의 다음 줄"이 아니라 "1순위 선석과 가장 가까운
    # 적합 후보"를 먼저 시도한다 — 정렬만 바꾸고 아래 게이트 로직은 그대로 둔다.
    substitutes = _sort_substitutes_by_distance(candidate, substitutes)

    for sub in substitutes:
        # 수심을 모르는 선석은 대체 후보에서 뺀다.
        #
        # 1순위 탐색(find_eligible_berths)은 depth_m IS NULL 을 이미 제외하는데
        # ("모르면 추천하지 않는다") 이 대체 경로에만 그 게이트가 없었다. 그래서
        # 수심 미상 선석이 후보로 내려오면 아래 draught_margin_m 계산에서
        # None - float 로 터졌다 — 오케스트레이터 전체가 500 이 되어 종합 판정
        # 자체를 못 했다(2026-08-21 실측: 수심 미상 5개 선석, 그것을 가리키는
        # 대체 관계 9건 — SK5부두 하나가 SK 계열 6개 부두의 대체 후보였다).
        #
        # 안전 판단이 불가능한 선석을 추천하지 않는 것이 원래 원칙이므로,
        # 조용히 0 으로 채우지 않고 사유를 남기고 건너뛴다.
        if sub.get("depth_m") is None:
            trace.append(f"대체 후보 '{sub['wharf_name']}' 탈락: 수심 자료 없음(판단 불가)")
            continue
        if sub.get("to_depth_m") is not None and vessel.draught_m > sub["to_depth_m"]:
            trace.append(f"대체 후보 '{sub['wharf_name']}' 탈락: 흘수 {vessel.draught_m}m > 수심 {sub['to_depth_m']}m")
            continue
        if (
            sub.get("to_max_dwt") is not None
            and vessel.dwt_t is not None
            and vessel.dwt_t > sub["to_max_dwt"]
        ):
            trace.append(f"대체 후보 '{sub['wharf_name']}' 탈락: DWT {vessel.dwt_t} > 최대 {sub['to_max_dwt']}")
            continue
        if category is not None:
            shared = (sub.get("shared_products") or "").split("/")
            if category not in shared:
                trace.append(
                    f"대체 후보 '{sub['wharf_name']}' 탈락: 화물 카테고리 '{category}' 미취급 "
                    f"(공유화물: {sub.get('shared_products') or '없음'})"
                )
                continue

        # 점유 판정은 berth_assignment(우리 배정 기록)만 본다(2026-08-19,
        # find_berth_candidates와 동일 결정 — 위 주석 참고).
        res = await find_overlapping_reservations(
            db, berth_ids=[sub["berth_id"]], window_start=window_start,
            window_end=window_end, exclude_call_sign=vessel.call_sign,
        )
        if res.get(sub["berth_id"]):
            trace.append(f"대체 후보 '{sub['wharf_name']}'도 점유 중 - 다음 대체 탐색")
            continue

        trace.append(f"'{sub['wharf_name']}'(으)로 대체 배정 (공유화물: {sub.get('shared_products')})")

        # 2026-08-21 수정 — 대체 선석은 원래(점유 중이라 탈락한) 선석과 물리적으로
        # 다른 위치인데, 이전 코드는 candidate.adjacent_cargos(원래 선석의 인접
        # 화물)를 그대로 복사해 썼다. 실측 확인(달포부두 대체 -> 북신항 에너지부두):
        # 원래 선석의 이웃(정일컨부두·효성부두·온산1부두·온산2부두)과 대체 선석의
        # 실제 이웃(신항북방파제 에너지부두)이 완전히 다르다 — 안전관제가 엉뚱한
        # 화물을 검사하고 진짜 이웃은 아예 확인을 안 하는 결함이었다.
        # find_berth_candidates/build_candidate_for_wharf_name과 동일하게 대체
        # 선석 자신의 인접 화물을 새로 조회한다.
        adjacency_map = await find_adjacent_categories(neo4j_driver, berth_ids=[sub["berth_id"]])
        adjacent_wharf_names = list({
            entry["adjacent_wharf_name"]
            for entry in adjacency_map.get(sub["berth_id"], [])
            if entry.get("adjacent_wharf_name")
        })
        real_cargo_by_wharf = await _real_adjacent_cargo_by_wharf(db, adjacent_wharf_names)

        substitute_candidate = BerthCandidate(
            rank=candidate.rank,
            berth_id=sub["berth_id"],
            wharf_name=sub["wharf_name"],
            port_name=sub.get("port_name"),
            depth_m=sub["depth_m"],
            berth_group=sub.get("berth_group"),
            onsan_scope=bool(sub.get("onsan_scope")),
            draught_margin_m=sub["depth_m"] - vessel.draught_m,
            occupancy_status=OccupancyStatus.AVAILABLE,
            adjacent_cargos=_adjacent_cargos_for(
                adjacency_map.get(sub["berth_id"], []), real_cargo_by_wharf
            ),
            latitude=sub.get("latitude"),
            longitude=sub.get("longitude"),
        )
        return BerthResolution(path="대체", berth=substitute_candidate, trace=trace)

    if substitutes:
        trace.append("대체 후보 전부 탈락(제원 초과 또는 점유 중) -> 정박지 대기 탐색")
    else:
        trace.append("대체 가능 선석 없음(단독선석) -> 정박지 대기 탐색")

    anchorage_candidates = await find_anchorage_candidates(neo4j_driver)
    anchorage_row = select_anchorage_for_dwt(vessel.dwt_t, anchorage_candidates)
    if anchorage_row is None:
        if vessel.dwt_t is None:
            trace.append("선박 DWT 미상 -> 톤수별 정박지 매칭 불가 -> 제안불가")
        elif vessel.dwt_t >= VLCC_BUOY_DWT:
            trace.append(f"DWT {vessel.dwt_t} >= {VLCC_BUOY_DWT}(VLCC급) -> 정박지 대신 부이/외해 대기 필요 -> 제안불가")
        else:
            trace.append(f"DWT {vessel.dwt_t}에 맞는 정박지 없음 -> 제안불가")
        return BerthResolution(path="제안불가", trace=trace)

    trace.append(f"정박지 '{anchorage_row['name']}' 대기 배정")
    return BerthResolution(
        path="정박지대기",
        anchorage=AnchorageAssignment(
            anchorage_id=anchorage_row["anchorage_id"],
            name=anchorage_row["name"],
            tonnage_rule=anchorage_row.get("tonnage_rule"),
            latitude=anchorage_row.get("latitude"),
            longitude=anchorage_row.get("longitude"),
        ),
        trace=trace,
    )


# ---------------------------------------------------------------------------
# 대체 선석 **제안** (2026-09-21 복구, D4)
#
# [왜 다시 넣었나]
#   9/21 오전에 검증모드에서 재탐색 경로를 통째로 끊었다. 점유를 이유로 재탐색이
#   돌다가 "이미 잘 붙어 있는 배"를 부적합으로 만든 사고 때문이었다(D7BX/UTT부두).
#   그 조치 자체는 맞았지만 **추천까지 같이 날아갔다.** 둘은 다른 문제다.
#
#       점유 중이다        → 재탐색 사유가 아니다. 정보다.
#       흘수·기상·혼재 부적합 → **대체안을 내야 한다.** 그게 우리가 할 일이다.
#
#   9/17 회의 §1 이 정한 방향 C 가 정확히 이것이다 — "기존 배정은 그대로 따르되,
#   조건이 기준을 벗어나면 에이전트가 조치안(대체 선석·정박지 대기·하역 보류)을
#   만들고 그 조치를 할 **권한이 있는 곳에 근거와 함께 넘긴다**."
#
# [배정과 추천의 경계]
#   여기서 나오는 것은 **의견**이다. 어떤 행도 만들지 않고, 어떤 자리도 잠그지
#   않는다(berth_assignment 는 2026-09-21 에 표째 삭제됐다). assessment_history 의
#   action_detail 에 담겨 화면에 뜨고, 실제로 옮길지는 선석회의·VTS·터미널이 정한다.
# ---------------------------------------------------------------------------


async def suggest_alternative_berths(
    db: AsyncSession,
    neo4j_driver: AsyncDriver,
    *,
    cargo: CargoRef,
    vessel: VesselSpec,
    window_start: datetime,
    window_end: datetime,
    exclude_wharf_name: str | None = None,
    draught_margin_m: float = 1.0,
    limit: int = 3,
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

    min_depth = vessel.draught_m + draught_margin_m - tide_m
    eligible = await find_eligible_berths(
        neo4j_driver, category=category, min_depth=min_depth, dwt_t=vessel.dwt_t,
    )

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
            f"'{category}'를 취급하면서 가용수심 조건(해도 {min_depth:.2f}m 이상)을 "
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
    real_cargo_by_wharf = await _real_adjacent_cargo_by_wharf(db, adjacent_wharf_names)

    candidates: list[BerthCandidate] = []
    for row in eligible:
        occupants = live_map.get(row["wharf_name"], [])
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
                    adjacency_map.get(row["berth_id"], []), real_cargo_by_wharf
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
