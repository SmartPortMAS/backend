"""카테고리 -> 대표 화학물질 매핑.

인접 선석의 "현재 취급 화물"을 안전관제 에이전트 입력으로 자동 채우려면 실제
최근 하역 화물 기록이 필요하지만, UN번호가 포함된 화물 상세 정보(울산항만공사
통합화물 API)는 선박별로 키(callsgn/연도/항차)를 미리 알아야 조회되는 API라
대량 백필이 불가능해 아직 수집되어 있지 않다 (upa_cargo_manifest 테이블 없음).

1차 구현에서는 인접 선석이 속한 카테고리(원유/유류/액체화학)를 대표하는
화학물질 1종으로 근사한다. data-pipeline/data_pipeline/loaders/cargo_category_loader.py
의 CARGO_CATEGORIES와 같은 3개 카테고리를 쓰되, 여기서는 chem_id 목록 전체가
아니라 각 카테고리의 대표 1종만 필요하다.

알려진 한계: 실제 인접 선석의 당일 취급 화물이 아니라 카테고리 대표값이다.
upa_cargo_manifest 파이프라인이 생기면 이 모듈 대신 실제 최근 하역 기록을
조회하도록 교체해야 한다.
"""

REPRESENTATIVE_CHEM_BY_CATEGORY: dict[str, str] = {
    "원유": "000751",     # 석유(PETROLEUM)
    "유류": "000973",     # 디젤 연료
    "액체화학": "001008",  # 벤젠
}


def representative_chem_id(category: str) -> str | None:
    return REPRESENTATIVE_CHEM_BY_CATEGORY.get(category)
