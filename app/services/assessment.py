"""판정 기록 서비스 (D1) — 오케스트레이터 결과를 `assessment_history` 한 행으로.

이 모듈이 방향 C 의 번역기다. 오케스트레이터는 아직 **배정 주체의 어휘**로
말한다(`승인가능`/`적합선석없음`/`전후보배정불가`). 우리가 화면·게이트·보고서에
쓰는 어휘는 **판정자의 어휘**다(`적합`/`주의`/`부적합`/`판정불가`). 여기서 옮긴다.

  OverallDecision.APPROVED  = "이 배를 이 선석에 넣어도 된다"   ← 배정 허가
  AssessmentLevel.FIT       = "지금 배정된 자리가 조건에 맞는다" ← 사실 확인

두 문장은 같은 계산에서 나오지만 **하는 말이 다르다.** 전자는 우리가 할 수 없는
말이고 후자는 우리가 해야 하는 말이다.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator.schemas import OrchestratorResult, OverallDecision
from app.agents.weather.schemas import WorkStatus
from app.models.assessment_history import (
    AssessmentAction,
    AssessmentHistory,
    AssessmentLevel,
    AssessmentRecipient,
    AssessmentStage,
)

logger = logging.getLogger("assessment")


# ── 시점 판정 ────────────────────────────────────────────────────────────────
#
# AIS 항해상태 하나만 본다. PORT-MIS 를 쓰지 않는 이유는 모델 주석 참고
# (수집창 [어제, 오늘+3일] 밖이면 동결 — 2026-09-21 실측 79척 중 32척).

def stage_from_nav_status(nav_status_code: str | None) -> AssessmentStage | None:
    """UPA 항해상태 → 시점. 상태를 모르면 None(= 판정불가로 기록)."""
    nav = (nav_status_code or "").strip()
    if not nav:
        return None
    if nav.startswith("정박(계류)"):
        return AssessmentStage.DURING_CARGO
    if nav.startswith("정박(앵커링)"):
        return AssessmentStage.BEFORE_BERTHING
    return AssessmentStage.BEFORE_ARRIVAL


# ── 조치안을 받을 곳 ─────────────────────────────────────────────────────────
#
# 9/17 회의 §3 이 조사한 그대로다. 울산항은 결정 주체가 셋으로 나뉘어 있고,
# 시점이 곧 주체를 정한다 — 같은 '부적합'이라도 입항 전이면 선석회의가, 계류
# 중이면 터미널이 받는다. 1차 보고서가 이 셋을 "VTS 가 승인"으로 뭉갠 것이
# 근본 오류였다(회의 §2).

_RECIPIENT_BY_STAGE = {
    AssessmentStage.BEFORE_ARRIVAL: AssessmentRecipient.BERTH_OPERATOR,
    AssessmentStage.BEFORE_BERTHING: AssessmentRecipient.VTS,
    AssessmentStage.DURING_CARGO: AssessmentRecipient.TERMINAL,
}

_ACTION_BY_STAGE = {
    AssessmentStage.BEFORE_ARRIVAL: AssessmentAction.ALTERNATIVE_BERTH,
    AssessmentStage.BEFORE_BERTHING: AssessmentAction.HOLD_ARRIVAL,
    AssessmentStage.DURING_CARGO: AssessmentAction.HOLD_CARGO,
}


def level_from_decision(result: OrchestratorResult) -> tuple[AssessmentLevel, str]:
    """오케스트레이터 귀결 → 판정 등급. 두 번째 값은 사람이 읽는 한 줄."""
    decision = result.overall_decision

    if decision is OverallDecision.APPROVED:
        # 기상이 '정상'이 아니면 통과라도 주의다 — 하역중단 임계를 넘지 않았을 뿐
        # 여유가 없다는 뜻이다. 판단불가면 '모르면 닫는다'(원칙 1).
        weather = result.weather_assessment
        if weather.status is WorkStatus.UNKNOWN:
            return AssessmentLevel.UNKNOWN, "기상 관측이 없거나 오래돼 판단할 수 없습니다"
        if weather.status is not WorkStatus.NORMAL:
            return AssessmentLevel.CAUTION, f"배정된 선석은 조건에 맞으나 기상이 '{weather.status.value}'입니다"
        return AssessmentLevel.FIT, "배정된 선석이 이 선박·화물 조건에 맞습니다"

    if decision is OverallDecision.WEATHER_BLOCKED:
        return AssessmentLevel.UNFIT, "기상 조건이 작업 한계를 넘었습니다"

    if decision is OverallDecision.NO_ELIGIBLE_BERTH:
        # ★ 같은 귀결이라도 **근거 부족**과 **실제 부적합**을 갈라야 한다.
        #   회의 §4 "근거 부족을 안전과 구분".
        #
        # 검증모드에서 이 귀결이 나오는 경로는 둘이다.
        #   (가) 근거 부족 — PORT-MIS 표기를 선석 마스터에서 못 찾았다 / 수심
        #        자료가 없다 / 체류 구간 조위 예보가 없다. 배가 위험하다는 뜻이
        #        전혀 아니다. **우리가 모른다**는 뜻이다.
        #   (나) 실제 부적합 — 가용수심 대비 흘수가 정말 모자란다.
        #
        # 실측으로 이 구분이 필요했다(2026-09-21). '장생포호안' 배정 4척이 전부
        # '부적합'으로 찍혔는데, 장생포호안은 정규화 해소가 안 되는 시설이다
        # (신항남방파제T/S부두 01 과 함께 알려진 미해소 2건). 부적합으로 두면
        # 관제사는 "이 배에 문제가 있다"로 읽는다. 실제로 필요한 조치는 선박
        # 점검이 아니라 **별칭 사전 보강**이다.
        detail = result.assignment_trace[0] if result.assignment_trace else None
        if result.evidence_missing:
            return AssessmentLevel.UNKNOWN, detail or "판정에 필요한 근거가 없습니다"
        return AssessmentLevel.UNFIT, detail or "배정된 선석이 이 선박 조건에 맞지 않습니다"

    if decision is OverallDecision.ALL_CANDIDATES_UNSAFE:
        # 검증모드에서 이 귀결은 "다른 선석이 없다"가 아니라 **"배정된 그 선석이
        # 이 배에 안 맞는다"** 는 뜻이다. 대상이 한 곳뿐이기 때문이다.
        return AssessmentLevel.UNFIT, "배정된 선석이 이 선박·화물 조건에 맞지 않습니다"

    if decision is OverallDecision.WAITING_ANCHORAGE:
        # 검증모드에서는 원래 나오지 않는 귀결이다(대상이 선석 하나로 고정).
        # 방어적으로 둔다 — 나오면 의견일 뿐 우리가 정박지로 보내지 않는다.
        return AssessmentLevel.CAUTION, "지금 선석보다 정박지 대기가 적절해 보입니다"

    return AssessmentLevel.UNKNOWN, f"판정 결과를 해석할 수 없습니다({decision.value})"


def _axes_from_result(result: OrchestratorResult) -> dict:
    """축별 등급·근거. 값이 없는 축은 넣지 않는다 — 빈 축과 '통과한 축'은 다르다."""
    axes: dict = {}

    weather = result.weather_assessment
    axes["기상"] = {
        "status": weather.status.value,
        "assessed_at_utc": weather.assessed_at_utc.isoformat(),
        "reasons": list(weather.reasons),
    }
    if weather.thresholds_used is not None:
        axes["기상"]["berth_group"] = getattr(weather.thresholds_used, "berth_group", None)

    if result.selected_berth is not None:
        b = result.selected_berth
        axes["흘수"] = {
            "wharf_name": b.wharf_name,
            "depth_m": b.depth_m,
            "margin_m": b.draught_margin_m,
        }

    if result.safety_assessment is not None:
        s = result.safety_assessment
        axes["혼재"] = {
            "target_cargo": s.target_cargo_name,
            "risk_level": s.risk_level.value,
            "conflict_count": len(s.conflicts) + len(s.imdg_conflicts),
            "key_hazards": list(s.key_hazards[:5]),
        }

    # '점유' 축은 일부러 비워 둔다.
    #   ① 우리는 선석을 점유하지 않는다(방향 C) — 우리 표의 예약을 점유라고 셀 수 없다.
    #   ② 오경보 백테스트 S2 가 점유 초과를 **정보로 강등**했다. PORT-MIS 출항 시각이
    #      예정값이라 동시 계류를 과대 계산한다(2026-09-21 실측 7/125 = 5.6%).
    # 실제 점유는 mart.vessel_presence(선박 위치 판정)가 관측으로 보여준다.
    return axes


def _alternatives_detail(result: OrchestratorResult) -> dict | None:
    """대체 선석 **제안**. 회의 §3 의 조치안 '대체선석'이다.

    배정이 아니다 — 어떤 자리도 잠그지 않는다. 관제사가 읽고 선석회의·VTS·
    터미널에 넘길 근거일 뿐이다. 그래서 키 이름도 assignment 가 아니라 suggestion 이다.
    """
    if not result.suggested_alternatives:
        return {"suggested": [], "note": result.suggestion_note} if result.suggestion_note else None
    return {
        "suggested": [
            {
                "rank": c.rank,
                "wharf_name": c.wharf_name,
                "berth_id": c.berth_id,
                "port_name": c.port_name,
                "depth_m": c.depth_m,
                # 해도 수심이 아니라 **체류 중 최저 조위를 더한 가용수심** 기준 여유다
                # (suggest_alternative_berths 참고) — 검증과 같은 잣대를 쓴다.
                "margin_m": round(c.draught_margin_m, 2),
                "occupancy": c.occupancy_status.value,
            }
            for c in result.suggested_alternatives
        ],
        "note": result.suggestion_note,
        "disclaimer": "제안입니다. 선석 배정 권한은 선석회의·VTS·터미널에 있습니다.",
    }


def _reasons_from_result(result: OrchestratorResult, headline: str) -> list[str]:
    reasons = [headline]
    reasons.extend(result.assignment_trace)
    reasons.extend(result.weather_assessment.reasons)
    if result.suggested_alternatives:
        names = ", ".join(
            f"{c.wharf_name}(여유 {c.draught_margin_m:.1f}m)"
            for c in result.suggested_alternatives
        )
        reasons.append(f"대체 선석 제안: {names} — 제안이며 배정이 아닙니다")
    elif result.suggestion_note:
        reasons.append(f"대체 선석을 제안하지 못했습니다: {result.suggestion_note}")
    if result.assignment_changed:
        reasons.append("배정된 선석과 다른 선석이 더 적합해 보입니다 — 의견이며 배정 변경이 아닙니다")
    # 중복 제거하되 순서는 유지한다(관제사가 읽는 순서가 근거의 우선순위다).
    seen: set[str] = set()
    return [r for r in reasons if r and not (r in seen or seen.add(r))]


_QUERY_LAST = text("""
    SELECT level, stage, action,
           -- 직전 제안의 계선시설 목록. 등급이 같아도 **추천이 바뀌면 기록해야** 한다.
           COALESCE(
               (SELECT string_agg(x->>'wharf_name', ',' ORDER BY x->>'rank')
                FROM jsonb_array_elements(action_detail->'alternatives'->'suggested') AS x),
               ''
           ) AS suggested_key
    FROM assessment_history
    WHERE call_sign = :call_sign
    ORDER BY assessed_at_utc DESC
    LIMIT 1
""")


async def record_assessment(
    db: AsyncSession,
    *,
    call_sign: str,
    vessel_name: str | None,
    stage: AssessmentStage | None,
    wharf_name: str | None,
    level: AssessmentLevel,
    headline: str,
    axes: dict | None = None,
    reasons: list[str] | None = None,
    action: AssessmentAction | None = None,
    action_detail: dict | None = None,
    input_snapshot: dict | None = None,
    suggested_key: str = "",
) -> bool:
    """판정 1건을 남긴다. 실제로 기록했으면 True.

    **같은 시점·같은 등급·같은 조치안·같은 제안이면 기록하지 않는다.**
    watch_arrivals 는 10분마다 도는데 매번 쓰면 하루 144행이 한 배에 쌓여
    타임라인이 읽히지 않는다. 우리가 보고 싶은 건 상태가 아니라 **변화**다
    (회의 §5 "시점별 재호출 · 변화 추적").

    제안(대체 선석)을 키에 넣은 이유 — 등급이 '부적합' 그대로여도 **어느 선석을
    권하는지가 바뀌면 그건 관제사에게 새 정보다.** 빈 자리 사정이나 기상이 달라져
    추천이 바뀌었는데 기록이 안 되면, 화면은 몇 시간 전 추천을 계속 보여준다.
    기상 수치처럼 매 주기 흔들리는 값은 일부러 키에서 뺐다 — 그걸 넣으면 10분마다
    새 행이 쌓여 원래 막으려던 문제로 되돌아간다.
    """
    stage_value = stage.value if stage is not None else AssessmentStage.BEFORE_ARRIVAL.value
    action_value = action.value if action is not None else None

    last = (await db.execute(_QUERY_LAST, {"call_sign": call_sign})).mappings().first()
    if last is not None and last["stage"] == stage_value and last["level"] == level.value \
            and last["action"] == action_value:
        return False

    recipient = _RECIPIENT_BY_STAGE.get(stage) if (stage is not None and action is not None) else None

    db.add(AssessmentHistory(
        call_sign=call_sign,
        vessel_name=vessel_name,
        stage=stage_value,
        wharf_name=wharf_name,
        level=level.value,
        axes=axes or {},
        reasons=reasons or [headline],
        action=action_value,
        action_detail=action_detail,
        recipient=recipient.value if recipient is not None else None,
        changed_from=last["level"] if last is not None else None,
        input_snapshot=input_snapshot,
        assessed_at_utc=datetime.now(timezone.utc),
    ))
    return True


async def record_from_orchestrator(
    db: AsyncSession,
    *,
    call_sign: str,
    vessel_name: str | None,
    stage: AssessmentStage | None,
    wharf_name: str | None,
    result: OrchestratorResult,
    input_snapshot: dict | None = None,
) -> bool:
    """오케스트레이터 결과를 그대로 판정 1건으로 옮긴다."""
    level, headline = level_from_decision(result)

    # 조치안은 **판정이 섰고 그 판정이 '적합'이 아닐 때만** 붙는다.
    # 받는 곳은 시점이 정한다(회의 §3).
    #
    # '판정불가'에는 조치안을 달지 않는다. 근거가 없어서 판단을 못 한 건인데
    # "대체 선석을 검토하라"고 권하면 근거 없는 조언이 된다 — 실제로 필요한 건
    # 배를 옮기는 게 아니라 **빠진 근거를 채우는 것**이다(표기 미해소면 별칭 사전,
    # 흘수 미상이면 선박제원). 등급 자체가 그 사실을 이미 말하고 있다.
    actionable = level in (AssessmentLevel.UNFIT, AssessmentLevel.CAUTION)
    action = _ACTION_BY_STAGE.get(stage) if (actionable and stage is not None) else None

    action_detail = None
    if action is not None:
        action_detail = result.decision_detail()
        alternatives = _alternatives_detail(result)
        if alternatives is not None:
            action_detail["alternatives"] = alternatives

    return await record_assessment(
        db,
        call_sign=call_sign,
        vessel_name=vessel_name,
        stage=stage,
        wharf_name=wharf_name,
        level=level,
        headline=headline,
        axes=_axes_from_result(result),
        reasons=_reasons_from_result(result, headline),
        action=action,
        action_detail=action_detail,
        input_snapshot=input_snapshot,
        suggested_key=",".join(
            c.wharf_name for c in sorted(result.suggested_alternatives, key=lambda x: x.rank)
        ),
    )
