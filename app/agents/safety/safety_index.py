"""다차원 안전 평가 지수 — 관제 화면 레이더 차트용.

[왜 축을 이렇게 잡았나]
원래 화면의 6축은 탱크 압력·배관 유속·가스 농도·온도 제어·작업자 안전이었다.
전부 현장 센서가 있어야 나오는 값인데 우리는 그 센서를 수집하지 않는다. 값이
없는 축을 그럴듯한 숫자로 채우면 화면 전체의 신뢰가 무너지므로, **지금 실제로
계산할 수 있는 안전 차원**으로 축을 바꿨다.

여섯 축은 모두 "이 값이 나쁘면 하역을 멈추거나 배정을 바꿔야 하는가"를 기준으로
골랐다. 각 축은 점수(0~100)와 함께 **그 점수가 나온 원자료**를 같이 돌려준다 —
관제사가 숫자만 보고 판단하지 않도록.

[모르면 비운다]
계산할 재료가 없으면 score 를 None 으로 둔다. 0(=최악)도 100(=안전)도 아니다.
화면은 이 축을 "판정 불가"로 표시해야 하며, 종합 점수 평균에서도 제외된다.
"""

from datetime import datetime, timezone

from neo4j import AsyncDriver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .berth_alerts import build_berth_alerts

# 전역 폴백 임계값(berth_weather_threshold.__GLOBAL_DEFAULT__)을 기준으로 여유를 본다.
# 선석마다 임계가 다르지만(정일 17m/s, OTK 14m/s) 이 지수는 항만 전체 한 장이라
# 가장 보수적인 공통 기준을 쓴다 — 개별 선석 판정은 /weather/assess 소관이다.
_Q_GLOBAL_THRESHOLD = text("""
    SELECT stop_wind_ms, stop_wave_m
    FROM berth_weather_threshold
    WHERE berth_group = '__GLOBAL_DEFAULT__'
""")

_Q_WEATHER = text("SELECT wind_speed_ms, wave_height_sig_m FROM mart.weather_now")

_Q_DRAUGHT = text("""
    SELECT draught_verdict, count(*) AS n
    FROM mart.berth_draught_check
    GROUP BY draught_verdict
""")

_Q_CARGO_ID = text("""
    SELECT count(*) FILTER (WHERE msds_matched) AS matched, count(*) AS total
    FROM mart.berth_current_cargo
""")

_Q_PIPELINE = text("SELECT collect_age_min FROM mart.pipeline_health")

# 선석 해소율 — "접안 신고 중 몇 %가 우리 선석 마스터로 해소되는가".
#
# [2026-09-22] 분모를 upa_port_call(이름 매칭) -> portmis_vessel(코드 매칭)으로
# 옮기고, 정박지를 분모에서 뺐다. 세 가지를 실측으로 확인한 뒤 내린 결정이다.
#
# ① 왜 upa_port_call 이 아닌가
#    그 표는 VTS **사후 이력**이라 분모가 30,205건(과거 전체)이다. 이 축이 답해야
#    할 질문은 "지금 들어오는 배를 선석까지 특정하고 있나"이므로 모집단이 틀렸다.
#    PORT-MIS 신고(현재·예정)가 맞는 모집단이고 240건이다.
#
# ② 왜 정박지를 빼는가
#    이 축은 아래 주석대로 **인접 혼재를 따질 수 있나**를 재는 축이다. 혼재 판정의
#    입력인 mart.berth_current_cargo 에 정박지 행은 0건이다(실측) — 묘박 중인 배는
#    안벽 인접이라는 개념 자체가 없다. 그런데 정박지 신고(WAE·WAM)가 분모의 57%
#    (312/552)를 차지해, 영원히 해소될 수 없는 건이 점수를 절반으로 눌렀다.
#    모집단을 선석 신고로 좁히는 것이지, 실패를 숨기는 것이 아니다.
#
# ③ KNOWN_GAP 은 분모에 남긴다
#    흘수 축이 UNKNOWN 을 빼는 것과 달리 여기서는 빼지 않는다. 저쪽은 위험도 축이라
#    "모르는 것을 안전으로 세지 않기" 위해 빼지만, 이 축은 **커버리지** 축이다.
#    해소 못 하는 것을 분모에서 빼면 "해소 가능한 것 중 몇 %를 해소하나"가 되어
#    늘 ~98% 가 나온다(실측 175/179) — 축이 스스로를 증명하는 숫자가 된다.
#    대신 몇 건이 왜 안 되는지는 basis 에 적어 드러낸다.
#
# 실측 대조(2026-09-22, 같은 시점):
#    옛 방식(upa_port_call x facility_alias)      13,526/30,205 = 44.8%
#    코드매칭, 정박지 포함                            175/552   = 31.7%
#    코드매칭, 정박지 제외  ← 채택                    175/240   = 72.9%
#    코드매칭, 정박지+KNOWN_GAP 제외                  175/179   = 97.8%
#
# ※ sub_code 는 양쪽 다 '01' 형태(0 패딩 2자리)여야 붙는다. 원천 PORT-MIS 가 그
#   형태로 주고, common_pg_loader.TEXT_ID_COLUMNS 가 그대로 보존한다. 한동안
#   적재 과정에서 앞자리 0 이 벗겨져 조인이 통째로 끊겼던 적이 있다(그쪽 주석).
_Q_BERTH_RESOLVED = text("""
    SELECT count(*) FILTER (WHERE m.match_level IN ('BERTH', 'WHARF')) AS berth_n,
           count(*) FILTER (WHERE m.match_level = 'KNOWN_GAP')         AS known_gap_n,
           count(*)                                                    AS total_n
    FROM portmis_vessel pv
    LEFT JOIN portmis_facility_map m
           ON m.facility_cd       = pv.arrival_facility_cd
          AND m.facility_sub_code IS NOT DISTINCT FROM pv.arrival_facility_sub_code
    WHERE pv.arrival_facility_cd IS NOT NULL
      -- 정박지(WAE·WAM)는 선석이 아니다 — 위 ② 참고.
      AND pv.arrival_facility_cd NOT LIKE 'WA%'
""")


def _axis(subject: str, score: float | None, basis: str) -> dict:
    return {
        "subject": subject,
        "score": None if score is None else round(max(0.0, min(100.0, score)), 1),
        "basis": basis,
    }


async def build_safety_index(db: AsyncSession, driver: AsyncDriver) -> dict:
    axes: list[dict] = []

    # 1. 기상 여유 — 임계값까지 얼마나 남았나 (풍속·파고 중 나쁜 쪽)
    th = (await db.execute(_Q_GLOBAL_THRESHOLD)).mappings().first()
    wx = (await db.execute(_Q_WEATHER)).mappings().first()
    if th and wx and (wx["wind_speed_ms"] is not None or wx["wave_height_sig_m"] is not None):
        used = []
        if wx["wind_speed_ms"] is not None and th["stop_wind_ms"]:
            used.append(wx["wind_speed_ms"] / float(th["stop_wind_ms"]))
        if wx["wave_height_sig_m"] is not None and th["stop_wave_m"]:
            used.append(wx["wave_height_sig_m"] / float(th["stop_wave_m"]))
        worst = max(used) if used else None
        axes.append(_axis(
            "기상 여유",
            None if worst is None else (1 - worst) * 100,
            f"풍속 {wx['wind_speed_ms']} m/s (중단 {th['stop_wind_ms']}), "
            f"파고 {wx['wave_height_sig_m']} m (중단 {th['stop_wave_m']})",
        ))
    else:
        axes.append(_axis("기상 여유", None, "관측값 없음 — 판정 불가"))

    # 2. 흘수 여유 — 접안 불가/경계 판정이 얼마나 섞여 있나.
    #    UNKNOWN(부두 제원 미확보)과 CHECK(선석별 수심이 달라 어느 선석인지 확인 필요)는
    #    분모에서 뺀다. 모르는 것을 안전으로도 위험으로도 세지 않기 위해서다 —
    #    대신 basis 에 몇 건인지 밝힌다.
    rows = {r["draught_verdict"]: r["n"] for r in (await db.execute(_Q_DRAUGHT)).mappings()}
    judged = rows.get("OK", 0) + rows.get("MARGINAL", 0) + rows.get("NOT_ALLOWED", 0)
    if judged:
        penalty = rows.get("NOT_ALLOWED", 0) + 0.5 * rows.get("MARGINAL", 0)
        axes.append(_axis(
            "흘수 여유", (1 - penalty / judged) * 100,
            f"판정 {judged}건 중 접안불가 {rows.get('NOT_ALLOWED', 0)}·"
            f"경계 {rows.get('MARGINAL', 0)} (제원 미확보 {rows.get('UNKNOWN', 0)}건·"
            f"선석 확인 요청 {rows.get('CHECK', 0)}건 제외)",
        ))
    else:
        axes.append(_axis(
            "흘수 여유", None,
            f"판정 가능한 접안 없음 (제원 미확보 {rows.get('UNKNOWN', 0)}건·"
            f"선석 확인 요청 {rows.get('CHECK', 0)}건)",
        ))

    # 3. 혼재 안전 — 규칙엔진이 실제로 잡아낸 격리 위반이 있는 선석 비율
    alerts = await build_berth_alerts(db, driver)
    seg_berths = {a["berth_name"] for a in alerts if a["type"] == "SEGREGATION" and a["berth_name"]}
    cargo_berths = (await db.execute(text(
        "SELECT count(DISTINCT facility_name) AS n FROM mart.berth_current_cargo "
        "WHERE facility_name IS NOT NULL"
    ))).scalar()
    if cargo_berths:
        axes.append(_axis(
            "혼재 안전", (1 - len(seg_berths) / cargo_berths) * 100,
            f"화물 재항 {cargo_berths}개 선석 중 격리 위반 {len(seg_berths)}개",
        ))
    else:
        axes.append(_axis("혼재 안전", None, "재항 화물 없음 — 판정 대상 없음"))

    # 4. 화물 식별 — 물질을 특정하지 못한 화물이 있으면 혼재 판정 자체가 불가능하다.
    cid = (await db.execute(_Q_CARGO_ID)).mappings().first()
    if cid and cid["total"]:
        axes.append(_axis(
            "화물 식별", 100.0 * cid["matched"] / cid["total"],
            f"MSDS 매칭 {cid['matched']}/{cid['total']}건 "
            f"(미확인 {cid['total'] - cid['matched']}건은 혼재 판정 불가)",
        ))
    else:
        axes.append(_axis("화물 식별", None, "재항 화물 없음"))

    # 5. 선석 특정 — 어느 선석에 붙었는지 모르면 인접 혼재를 따질 수 없다.
    br = (await db.execute(_Q_BERTH_RESOLVED)).mappings().first()
    if br and br["total_n"]:
        gap = br["known_gap_n"]
        unmapped = br["total_n"] - br["berth_n"] - gap
        axes.append(_axis(
            "선석 특정", 100.0 * br["berth_n"] / br["total_n"],
            f"접안 신고 {br['total_n']}건 중 선석 확정 {br['berth_n']}건 "
            f"(제원 공시 없음 {gap}건: 호안·의장안벽·공사중 / 미매핑 {unmapped}건. "
            f"정박지 신고는 분모에서 제외)",
        ))
    else:
        axes.append(_axis("선석 특정", None, "접안 신고 없음"))

    # 6. 데이터 신선도 — 위 판정들이 언제 값으로 내려진 것인가.
    #    수집 주기가 1시간이라 60분까지는 만점, 3시간(기상 판정 MAX_STALENESS)에서 0.
    age = (await db.execute(_Q_PIPELINE)).scalar()
    if age is None:
        axes.append(_axis("데이터 신선도", None, "수집 이력 없음"))
    else:
        age = float(age)
        score = 100.0 if age <= 60 else (1 - (age - 60) / 120) * 100
        axes.append(_axis(
            "데이터 신선도", score,
            f"마지막 수집 {int(age)}분 전 (수집 주기 60분, 180분 초과 시 0점)",
        ))

    scored = [a["score"] for a in axes if a["score"] is not None]
    return {
        "axes": axes,
        "overall": round(sum(scored) / len(scored), 1) if scored else None,
        "unavailable_axes": [a["subject"] for a in axes if a["score"] is None],
        "computed_at_utc": datetime.now(timezone.utc),
    }
