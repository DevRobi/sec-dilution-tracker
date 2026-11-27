from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import os


def _load_dotenv_files() -> None:
    """Load repo-root and backend .env before reading DATABASE_URL (local dev)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    backend = Path(__file__).resolve().parents[1]  # backend/
    root = backend.parent
    load_dotenv(root / ".env")
    load_dotenv(backend / ".env", override=True)


_load_dotenv_files()

# Default to local postgres if not set (for dev)
# In production, this comes from the platform's DATABASE_URL (Render/Neon/RDS).
RAW_DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost/buyback_tracker"
)
SQL_ECHO = os.getenv("SQL_ECHO", "false").lower() in {"1", "true", "yes", "on"}


def _normalize_db_url(raw: str) -> tuple[str, dict]:
    """
    Make any standard Postgres URL work with SQLAlchemy's ASYNC asyncpg driver.

    Managed providers (Render, Neon, Heroku, RDS) hand out URLs like
    `postgres://...` or `postgresql://...?sslmode=require`, but:
      * the async engine needs the `postgresql+asyncpg://` driver prefix, and
      * asyncpg does NOT understand libpq query args like `sslmode` /
        `channel_binding` — they raise "unexpected keyword argument". SSL is
        passed via connect_args instead.
    sqlite/other URLs pass through untouched. Returns (url, connect_args).
    """
    connect_args: dict = {}
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    if raw.startswith("postgresql://"):
        raw = "postgresql+asyncpg://" + raw[len("postgresql://"):]
    if raw.startswith("postgresql+asyncpg://"):
        parts = urlsplit(raw)
        q = dict(parse_qsl(parts.query))
        sslmode = q.pop("sslmode", None)
        q.pop("channel_binding", None)  # asyncpg negotiates this itself
        if sslmode and sslmode != "disable":
            connect_args["ssl"] = True
        raw = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment)
        )
    return raw, connect_args


DATABASE_URL, _CONNECT_ARGS = _normalize_db_url(RAW_DATABASE_URL)

IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Read-only mode: point DATABASE_URL at a SQLite file with "?mode=ro&uri=true"
# (or set DB_READ_ONLY=true) to browse/audit a database without any writes.
# Startup then skips create_all, ticker-universe seeding and the cleanup sweep.
READ_ONLY = "mode=ro" in DATABASE_URL or os.getenv("DB_READ_ONLY", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Queue-pool sizing only applies to server databases. SQLite (especially
# ":memory:", used by tests) gets a StaticPool/NullPool from SQLAlchemy and
# rejects pool_size/max_overflow/pool_timeout with a TypeError at import time.
_POOL_KWARGS: dict = (
    {}
    if IS_SQLITE
    else {
        "pool_size": 20,
        "max_overflow": 10,
        "pool_timeout": 60,
        "pool_recycle": 1800,  # recycle connections every 30m to avoid stale/closed sockets
    }
)

engine = create_async_engine(
    DATABASE_URL,
    echo=SQL_ECHO,
    connect_args=_CONNECT_ARGS,
    pool_pre_ping=True,
    **_POOL_KWARGS,
)

AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

Base = declarative_base()

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
