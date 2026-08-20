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
    # ("가스" 카테고리는 만들지 않는다 — 2026-08-19 실측 확인, 아래 이유)
    # cargo_category_loader.py의 CARGO_CATEGORIES는 Neo4j Berth의 HANDLES 관계와
    # 이름이 정확히 일치해야 하는데, 그 관계의 유일한 소스인
    # upa_berth_facility.handling_cargo_name에는 "원유"/"유류"/"액체화학" 3개
    # 토큰만 존재한다(라이브 DB 직접 조회로 확인). "가스" 카테고리를 만들면
    # 그 카테고리로 분류된 화학물질은 HANDLES 관계가 있는 선석이 하나도 없어
    # 전부 "적합 선석 없음"이 된다. 가스부두의 handling_cargo_name도 "유류"이므로,
    # 가스류 화학물질(수소·암모니아 등)은 cargo_category_loader.py에서 이미
    # "유류"로 분류돼 있다 — 이 표에 "가스" 키를 추가할 필요가 없다.
}


def representative_chem_id(category: str) -> str | None:
    return REPRESENTATIVE_CHEM_BY_CATEGORY.get(category)
