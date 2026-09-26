"""챗봇 물질 해석 — 한글명이 없는 등재 화물의 별칭 (2026-09-27)."""
from app.agents.chatbot.aliases import ALIAS_TO_CAS, normalize_name


def test_korean_alias_for_english_only_names():
    # 예전엔 벡터 매칭으로 넘어가 수소화 폴리데센(다른 물질)으로 해석됐다
    assert ALIAS_TO_CAS[normalize_name("1-도데센")] == "112-41-4"
    assert ALIAS_TO_CAS[normalize_name("2-프로필헵탄올")] == "10042-59-8"
    # 기존 '도데센'(분지형) 매핑은 그대로
    assert ALIAS_TO_CAS[normalize_name("도데센")] == "97280-83-6"
