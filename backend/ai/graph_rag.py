import logging
from simulator.data_generator import PROHIBITION_MATRIX, CARGO_MSDS_DB

logger = logging.getLogger(__name__)

def evaluate_safety(cargo_name: str, adjacent_cargo: str):
    """
    안전관제 에이전트 핵심 로직: 
    GraphRAG 대체(Mock) - 화물 간 혼재위험 평가
    """
    logger.info(f"Evaluating safety between {cargo_name} and {adjacent_cargo}")
    
    # 혼재 금지 확인
    prohibited_list = PROHIBITION_MATRIX.get(cargo_name, [])
    is_safe = adjacent_cargo not in prohibited_list
    
    reason = "안전: 인접 선석 화물과 혼재 위험이 없습니다."
    if not is_safe:
        reason = f"위험: '{cargo_name}'와 '{adjacent_cargo}'는 혼재 금지 물질입니다."

    # MSDS 기반 체크리스트
    checklist = []
    msds_info = CARGO_MSDS_DB.get(cargo_name)
    if msds_info:
        checklist = msds_info.get("안전체크리스트", [])
    
    return {
        "safe": is_safe,
        "reason": reason,
        "checklist": checklist,
        "msds_summary": {
            "hazard_class": msds_info.get("위험등급") if msds_info else "N/A",
            "un_no": msds_info.get("un_no") if msds_info else "N/A",
        }
    }

def get_cargo_knowledge_graph(cargo_name: str):
    """지식그래프 탐색을 위한 시각화 데이터 제공 (Mock)"""
    msds = CARGO_MSDS_DB.get(cargo_name, {})
    prohibited = msds.get("혼재금지", [])
    
    nodes = [{"id": cargo_name, "group": "Target Cargo"}]
    links = []
    
    for p_cargo in prohibited:
        nodes.append({"id": p_cargo, "group": "Prohibited Cargo"})
        links.append({"source": cargo_name, "target": p_cargo, "value": "혼재금지"})
        
    return {"nodes": nodes, "links": links}
