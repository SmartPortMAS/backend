"""벌크 액체화학물질 호환성 그룹(Compatibility Group) 참고 축 — 그래프 조회 결과 해석.

2026-08-21에 정적 Python dict 구현에서 Neo4j 그래프 조회로 전환했다. 배경:
챗봇(GraphRAG)이 이 축의 데이터를 전혀 보지 못해 "안전관제 에이전트는 위험하다고
막는 조합인데 챗봇은 정보 없음이라 답하는" 불일치가 있었다(전체 31개 위험 조합 중
10개, 32%가 다른 두 축으로는 전혀 안 잡히는 조합이었다) — 챗봇이 같은 정보를
그래프 탐색으로 볼 수 있으려면 데이터가 Neo4j에 있어야 했다. 정적 dict와 그래프,
두 곳에 같은 데이터를 유지하면 언젠가 어긋날 위험이 있어(실제로 이 프로젝트의
다른 축들도 전부 "판정 로직의 권위는 하나"라는 원칙을 따른다 — berth_alerts.py
모듈 docstring 참고), 안전관제 에이전트도 그래프 조회로 전환해 하나로 합쳤다.

[출처와 성격 — bulk_compatibility_neo4j_loader.py와 동일 내용]
그룹 번호 체계(반응성그룹 1~22 / 화물그룹 30~43)는 미국 해안경비대(USCG)
46 CFR Part 150 계열의 산적 액체화학물질 호환성 차트를 참고해 이 프로젝트가
관리하는 36종 화학물질의 CAS 번호 기준으로 재구성한 참고 데이터다. 국내에도
한국해사위험물검사원(KOMDI)이 운영하는 "산적케미칼 격리 툴(STBC)"이라는 유사
체계가 실재하나, 이 데이터가 STBC의 분류와 수치까지 정확히 일치하는지는 대조되지
않았다 — 참고 신호로만 다룬다. MSDS 텍스트 마이닝·IMDG 공인 격리표 두 축을
대체하지 않고 병렬로 추가하는 제3의 신호이며, 최종 하한은 세 축의 최댓값으로
결정된다(rule_engine.compute_bulk_compatibility_floor).
"""

from .schemas import RiskLevel


def build_bulk_conflicts(
    *,
    target_chem_id: str,
    adjacent_chem_ids: list[str],
    adjacent_names: dict[str, str],
    group_conflict_ids: set[str],
    groups_by_chem_id: dict[str, tuple[int, str, str]],
    safe_exception_ids: set[str],
    blocked_exception_ids: set[str],
) -> list[dict]:
    """그래프 조회 결과(graph_queries.find_bulk_group_conflicts/find_bulk_groups/
    find_bulk_exceptions)를 raw conflict dict 리스트로 정리한다.
    rule_engine.compute_bulk_compatibility_floor가 이 리스트를 소비한다.

    우선순위: 안전 예외(무조건 통과) > 차단 예외(무조건 배정불가) > 그룹 규칙
    위반(배정불가) > 특수가스(둘 중 하나라도 미분류 그룹이면 주의) > 그 외 무신호.
    두 화물의 그룹이 둘 다 확인됐는데 위 어디에도 안 걸리면 "이 참고축에서는
    충돌 근거 없음"일 뿐 "안전 확정"은 아니다 — 다른 두 축이 독립적으로 계속 적용된다.
    """
    target_group = groups_by_chem_id.get(target_chem_id)
    conflicts: list[dict] = []

    for adj_id in adjacent_chem_ids:
        if adj_id in safe_exception_ids:
            continue

        adj_group = groups_by_chem_id.get(adj_id)
        name = adjacent_names.get(adj_id, adj_id)

        if adj_id in blocked_exception_ids:
            conflicts.append({
                "chem_id": adj_id,
                "name_ko": name,
                "risk_level": RiskLevel.BLOCKED,
                "reason": "46 CFR Part 150 Appendix I(b) 성격의 예외 — 일반 그룹 차트의 공백을 메우는 강제 격리",
                "group_a": target_group,
                "group_b": adj_group,
            })
            continue

        if adj_id in group_conflict_ids:
            g_a = f"{target_group[0]}({target_group[2]})" if target_group else "미상"
            g_b = f"{adj_group[0]}({adj_group[2]})" if adj_group else "미상"
            conflicts.append({
                "chem_id": adj_id,
                "name_ko": name,
                "risk_level": RiskLevel.BLOCKED,
                "reason": f"호환성 그룹 {g_a}↔{g_b} 조합 — 격렬한 반응(발열·가스발생 등) 가능 조합으로 분류",
                "group_a": target_group,
                "group_b": adj_group,
            })
            continue

        if target_group and adj_group and (target_group[1] == "special" or adj_group[1] == "special"):
            conflicts.append({
                "chem_id": adj_id,
                "name_ko": name,
                "risk_level": RiskLevel.CAUTION,
                "reason": "표준 호환성 차트가 적용되지 않는 미분류 가스/특수물질 — 개별 전문가 검토 필요",
                "group_a": target_group,
                "group_b": adj_group,
            })

    return conflicts
