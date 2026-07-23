from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
import os

# PostgreSQL 연결 정보 (pgvector 포함)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost:5432/cargo_db")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
