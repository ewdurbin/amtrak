"""
SQLAlchemy models for Amtrak data
"""

import os
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Text,
    DateTime,
    Float,
    JSON,
    func,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class Station(Base):
    __tablename__ = "stations"

    code = Column(String(10), primary_key=True)
    name = Column(String(255), nullable=False)
    lat = Column(Float)
    lon = Column(Float)
    data = Column(JSON)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class Train(Base):
    __tablename__ = "trains"

    train_number = Column(String(20), primary_key=True)
    train_id = Column(String(50), primary_key=True)
    route_name = Column(String(255))
    departure_date = Column(DateTime)
    train_state = Column(String(20))  # Predeparture, Active, Completed
    stations_snapshot = Column(JSON)  # Snapshot of station data at time of trip
    data = Column(JSON)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class Metadata(Base):
    __tablename__ = "metadata"

    key = Column(String(50), primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


def get_database_url():
    """Get database URL from environment or use SQLite default"""
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        # SQLAlchemy uses postgresql:// not postgres://
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        # Use psycopg driver (psycopg3) instead of psycopg2
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
        return database_url
    else:
        database_path = os.environ.get("AMTRAK_DB_PATH", "amtrak_data.db")
        return f"sqlite:///{database_path}"


def get_engine():
    """Create and return SQLAlchemy engine"""
    database_url = get_database_url()
    if database_url.startswith("sqlite"):
        # SQLite specific settings
        return create_engine(database_url, connect_args={"check_same_thread": False})
    else:
        # PostgreSQL settings
        return create_engine(database_url, pool_pre_ping=True)


def init_db():
    """Initialize database tables"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session():
    """Get a database session"""
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()
