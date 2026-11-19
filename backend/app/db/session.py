from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings


_is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 30} if _is_sqlite else {}
# Mirror mode: the database file is swapped for a fresh clinic upload at any
# time, so never keep connections to the old file around.
_pool = {"poolclass": NullPool} if settings.mirror_mode else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True, pool_pre_ping=not _is_sqlite, **_pool)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # WAL lets every counter keep reading while one writes (default rollback-journal
        # mode blocks readers during writes); the rest trade a little durability on
        # power loss (NORMAL still survives app crashes) for far fewer fsyncs, and keep
        # hot pages/temp sorts in memory as the history tables grow.
        cur = dbapi_conn.cursor()
        # (A mirror copy stays in rollback-journal mode: a -wal file left next to
        # it would not match the next uploaded copy.)
        cur.execute("PRAGMA journal_mode=DELETE" if settings.mirror_mode else "PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA cache_size=-65536")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA mmap_size=268435456")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
