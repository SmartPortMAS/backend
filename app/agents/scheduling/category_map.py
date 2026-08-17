"""카테고리 -> 대표 화학물질 매핑 (실데이터 없을 때의 폴백).

2026-08-11 이전에는 인접 선석의 "현재 취급 화물"을 이 카테고리 근사값으로만
채웠다 — 화물 manifest(upa_cargo_manifest)가 아직 수집되어 있지 않았기 때문
(UPA 통합화물 API가 선박별 키를 미리 알아야 조회되는 구조라 대량 백필 불가).

지금은 그 전제가 해소됐다: upa_cargo_manifest 파이프라인이 생겼고(합성 데이터
위주지만 실제 선박·실제 선종 기반), mart.berth_current_cargo가 이걸 소비 계약
형태로 노출한다. scheduling/service.py의 `_adjacent_cargos_for()`가 이제
그 뷰를 1순위로 쓰고, 이 모듈은 **실데이터가 없는 인접 선석에 대해서만** 폴백
근사로 쓰인다 — data-pipeline/data_pipeline/loaders/cargo_category_loader.py의
CARGO_CATEGORIES와 같은 3개 카테고리를 쓰되, 여기서는 chem_id 목록 전체가
아니라 각 카테고리의 대표 1종만 필요하다.

알려진 한계: 여전히 근사치다(대표 1종일 뿐, 실제 그 배가 뭘 싣고 있는지는
모른다). 화물 manifest 커버리지(현재 대부분 합성 데이터)가 올라갈수록 이
폴백이 호출되는 비중은 자연히 줄어든다.
"""

REPRESENTATIVE_CHEM_BY_CATEGORY: dict[str, str] = {
    "원유": "000751",     # 석유(PETROLEUM)
    "유류": "000973",     # 디젤 연료
    "액체화학": "001008",  # 벤젠
    # 가스 카테고리 신설(2026-08-18, data-pipeline cargo_category_loader)에 맞춰 추가.
    # 여기 없으면 인접 선석이 가스부두일 때 폴백 근사 화물이 비어, 혼재 검사가
    # 그 선석만 조용히 건너뛴다.
    "가스": "015420",     # 프로페인
}


def representative_chem_id(category: str) -> str | None:
    return REPRESENTATIVE_CHEM_BY_CATEGORY.get(category)
