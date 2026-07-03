from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MsdsChemicalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chem_id: str
    cas_no: str | None
    un_no: str | None
    name_ko: str | None
    name_en: str | None
    quality_flag: str
    collected_at_utc: datetime
    msds_payload: dict
