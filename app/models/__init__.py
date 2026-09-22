"""ORM 모델 등록.

[2026-09-21] 배정 계열 모델 4개를 지웠다 — `BerthAssignment` · `AnchorageQueue` ·
`SchedulingExclusion` · `AisVesselPosition`. 앞의 셋은 우리가 배정 주체일 때만
말이 되는 표였고(방향 C), `AisVesselPosition` 은 UPA 선박위치로 대체된 레거시다
(9/17 회의 "AIS 테이블 제거 ✅ 찬성"). 지운 표는 alembic 0027 이 DROP 한다.

이 시스템의 산출물은 이제 `AssessmentHistory` 하나다.

(2026-08-19) Berth 모델·테이블 삭제됨 — upa_berth_facility.wharf_name 을 DB 레벨로
참조한다(0016). upa_* 는 data-pipeline 소유 표라 ORM 모델을 두지 않는다.
"""

from app.models.assessment_history import (
    GATE_BLOCKING_LEVELS,
    AssessmentAction,
    AssessmentHistory,
    AssessmentLevel,
    AssessmentRecipient,
    AssessmentStage,
)
from app.models.base import Base
from app.models.berth_weather_threshold import GLOBAL_DEFAULT_BERTH_GROUP, BerthWeatherThreshold
from app.models.environmental_obs import TideObs, WaveObs, WeatherForecast, WeatherObs
from app.models.msds_chemical import MsdsChemical
from app.models.msds_embedding import (
    CHUNK_KIND_IDENTITY,
    CHUNK_KIND_SECTION,
    MsdsEmbedding,
)
from app.models.portmis_vessel import PortmisVessel
from app.models.vessel_spec import VesselSpec

__all__ = [
    "Base",
    "MsdsChemical",
    "MsdsEmbedding",
    "CHUNK_KIND_IDENTITY",
    "CHUNK_KIND_SECTION",
    "PortmisVessel",
    "TideObs",
    "WaveObs",
    "WeatherObs",
    "WeatherForecast",
    "BerthWeatherThreshold",
    "GLOBAL_DEFAULT_BERTH_GROUP",
    "VesselSpec",
    "AssessmentHistory",
    "AssessmentStage",
    "AssessmentLevel",
    "AssessmentAction",
    "AssessmentRecipient",
    "GATE_BLOCKING_LEVELS",
]
