from sqlalchemy import Column, Integer, String, Float, DateTime
from database import Base
from pgvector.sqlalchemy import Vector

class Cargo(Base):
    __tablename__ = "cargos"

    id = Column(Integer, primary_key=True, index=True)
    un_number = Column(String, index=True)
    cargo_name = Column(String, index=True)
    hazard_class = Column(String)
    msds_summary = Column(String)
    
    # pgvector를 이용한 MSDS 임베딩 벡터 저장 (예: 1536 차원 OpenAI 임베딩)
    embedding = Column(Vector(1536))

class VesselLog(Base):
    __tablename__ = "vessel_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    vessel_name = Column(String, index=True)
    eta = Column(DateTime)
    berth_assigned = Column(String)
    status = Column(String)
