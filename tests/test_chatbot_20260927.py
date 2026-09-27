"""챗봇 — 한글명이 없는 등재 화물의 별칭, 답변 결론 등급 교정 (2026-09-27)."""
from app.agents.chatbot.aliases import ALIAS_TO_CAS, normalize_name


def test_korean_alias_for_english_only_names():
    # 예전엔 벡터 매칭으로 넘어가 수소화 폴리데센(다른 물질)으로 해석됐다
    assert ALIAS_TO_CAS[normalize_name("1-도데센")] == "112-41-4"
    assert ALIAS_TO_CAS[normalize_name("2-프로필헵탄올")] == "10042-59-8"
    # 기존 '도데센'(분지형) 매핑은 그대로
    assert ALIAS_TO_CAS[normalize_name("도데센")] == "97280-83-6"


# ── 답변 결론 등급이 판정 등급과 다르면 코드가 교정한다 (G5) ──

def _assessment(level):
    from types import SimpleNamespace
    from app.agents.safety.schemas import RiskLevel
    return SimpleNamespace(risk_level=RiskLevel(level))


def test_matching_conclusion_is_kept():
    from app.agents.chatbot.service import _enforce_assessment_level
    assert _enforce_assessment_level("배정불가입니다.", _assessment("배정불가"), "배정불가") == "배정불가입니다."


def test_mismatched_conclusion_gets_verdict_prefix():
    from app.agents.chatbot.service import _enforce_assessment_level
    # 실제 사례: 판정은 배정불가인데 답변이 '판단 불가'
    fixed = _enforce_assessment_level("판단할 수 없습니다.", _assessment("배정불가"), None)
    assert fixed.startswith("**혼재 판정: 배정불가**") and fixed.endswith("판단할 수 없습니다.")


def test_matching_conclusion_without_level_word_gets_prefix():
    from app.agents.chatbot.service import _enforce_assessment_level
    # 실제 사례(9/27): 결론 필드는 배정불가인데 본문은 "혼재가 불가합니다"뿐
    fixed = _enforce_assessment_level("혼재가 불가합니다.", _assessment("배정불가"), "배정불가")
    assert fixed.startswith("**혼재 판정: 배정불가**")


def test_no_assessment_no_change():
    from app.agents.chatbot.service import _enforce_assessment_level
    assert _enforce_assessment_level("벤젠의 인화점은 -11 ℃입니다.", None, None) == "벤젠의 인화점은 -11 ℃입니다."
