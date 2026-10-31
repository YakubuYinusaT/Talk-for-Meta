from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base


BUSY_SECONDS = 30  # how long a write waits for another write to finish before SQLite gives up with "database is locked" (the default is 5)


def make_engine(url: str):
    kwargs: dict = {}
    on_disk = False
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": BUSY_SECONDS}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        else:
            path = url.replace("sqlite:///", "", 1)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            on_disk = True
    engine = create_engine(url, future=True, **kwargs)
    if on_disk:
        @event.listens_for(engine, "connect")
        def wal(dbapi_connection, _record):
            """A database file on a server: readers never wait for a writer (WAL), and a writer waits its turn instead of failing, so a call's status
            message arriving while another turn is being worked on is not lost."""
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute(f"PRAGMA busy_timeout={BUSY_SECONDS * 1000}")
            cur.close()
    return engine


def init_db(engine) -> None:
    """Create tables. Alembic migrations replace this before production."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_product_aliases_phrase_trgm "
                    "ON product_aliases USING gin (phrase gin_trgm_ops)"
                )
            )


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
