from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from config import SQLALCHEMY_DATABASE_URL

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
)


@event.listens_for(engine, "connect")
def _tune_sqlite(dbapi_connection, connection_record):
    """
    Hardening for a long-running app with background jobs:

    - WAL lets readers keep working while a write is in progress, so a slow
      metric sync never blocks the dashboard.
    - busy_timeout makes a writer wait for the lock instead of failing
      immediately with "database is locked".
    - NORMAL synchronous is the recommended pairing with WAL: still crash-safe,
      far fewer fsyncs.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
