from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import get_settings

_settings = get_settings()
engine = create_engine(_settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
