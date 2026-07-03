"""KOSHA MSDS API 비동기 클라이언트.

data-pipeline/data_pipeline/collectors/msds_api_collector.py 의 배치용 동기 클라이언트와
동일한 API를 호출하지만, 요청 처리 경로(latency budget)에 맞춰 httpx.AsyncClient +
asyncio.gather 로 detail01~16 을 병렬 호출한다.
"""

import asyncio
import xml.etree.ElementTree as ET

import httpx

from app.config import get_settings
from app.core.exceptions import MsdsUpstreamError

BASE_URL = "https://apis.data.go.kr/B552468/msdschem"

DETAIL_ENDPOINTS: list[tuple[str, str, str]] = [
    ("detail01", "/getChemDetail01", "화학제품과 회사에 관한 정보"),
    ("detail02", "/getChemDetail02", "유해성·위험성"),
    ("detail03", "/getChemDetail03", "구성성분의 명칭 및 함유량"),
    ("detail04", "/getChemDetail04", "응급조치요령"),
    ("detail05", "/getChemDetail05", "폭발·화재시 대처방법"),
    ("detail06", "/getChemDetail06", "누출사고시 대처방법"),
    ("detail07", "/getChemDetail07", "취급 및 저장방법"),
    ("detail08", "/getChemDetail08", "노출방지 및 개인보호구"),
    ("detail09", "/getChemDetail09", "물리화학적 특성"),
    ("detail10", "/getChemDetail10", "안정성 및 반응성"),
    ("detail11", "/getChemDetail11", "독성에 관한 정보"),
    ("detail12", "/getChemDetail12", "환경에 미치는 영향"),
    ("detail13", "/getChemDetail13", "폐기시 주의사항"),
    ("detail14", "/getChemDetail14", "운송에 필요한 정보"),
    ("detail15", "/getChemDetail15", "법적 규제현황"),
    ("detail16", "/getChemDetail16", "그 밖의 참고사항"),
]

# 전체 조회(list + 16개 detail)에 허용하는 타임아웃 예산
REQUEST_TIMEOUT_SECONDS = 10.0


def _xml_to_items(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items_el = root.find(".//items")
    if items_el is None:
        return []
    return [
        {child.tag: (child.text or "").strip() for child in item}
        for item in items_el.findall("item")
    ]


async def fetch_chem_by_cas(client: httpx.AsyncClient, cas_no: str) -> dict | None:
    """CAS번호로 정확한 chemId 1건 조회 (searchCnd=1).

    이 호출이 실패하면(인증 실패, 타임아웃, 5xx 등) 요청 전체가 무의미해지므로
    MsdsUpstreamError로 감싸 API 레이어가 502로 응답할 수 있게 한다. 반대로
    detail01~16(fetch_full_msds_record)은 일부만 실패해도 나머지로 응답 가능하므로
    asyncio.gather(return_exceptions=True)로 개별 실패를 그냥 삼킨다.
    """
    settings = get_settings()
    try:
        r = await client.get(
            f"{BASE_URL}/getChemList",
            params={
                "serviceKey": settings.kosha_api_key,
                "searchWrd": cas_no,
                "searchCnd": 1,
                "numOfRows": 1,
                "pageNo": 1,
            },
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise MsdsUpstreamError(cas_no, f"HTTP {e.response.status_code}") from e
    except httpx.RequestError as e:
        raise MsdsUpstreamError(cas_no, f"요청 실패: {e}") from e

    items = _xml_to_items(r.text)
    return items[0] if items else None


async def fetch_chem_detail(client: httpx.AsyncClient, chem_id: str, endpoint: str) -> list[dict]:
    """chemId로 특정 섹션 전체 항목 조회 (msdsItemCode별 다건)."""
    settings = get_settings()
    r = await client.get(
        f"{BASE_URL}{endpoint}",
        params={
            "serviceKey": settings.kosha_api_key,
            "chemId": chem_id,
            "numOfRows": 100,
            "pageNo": 1,
        },
    )
    r.raise_for_status()
    return _xml_to_items(r.text)


async def fetch_full_msds_record(cas_no: str) -> dict | None:
    """CAS번호 1건에 대해 목록 조회 + 16개 섹션 상세 조회를 수행한다.

    detail 섹션은 asyncio.gather로 병렬 호출한다 — 배치 수집기처럼 순차 호출 +
    sleep을 쓰면 사용자 요청 하나당 수 초~십수 초가 걸려 API 응답 경로에 부적합하다.

    Returns:
        list_info + detail01~16 을 담은 raw record. CAS번호로 물질을 찾지 못하면 None.
    """
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        list_item = await fetch_chem_by_cas(client, cas_no)
        if not list_item:
            return None

        chem_id = list_item.get("chemId", "")

        detail_results = await asyncio.gather(
            *[fetch_chem_detail(client, chem_id, endpoint) for _, endpoint, _ in DETAIL_ENDPOINTS],
            return_exceptions=True,
        )

    record: dict = {
        "_query_cas_no": cas_no,
        "_chem_id": chem_id,
        "list_info": list_item,
    }
    for (key, _, section_name), result in zip(DETAIL_ENDPOINTS, detail_results):
        data = [] if isinstance(result, BaseException) else result
        record[key] = {"section": section_name, "data": data}

    return record
