"""Async SQLAlchemy engine and session factory (asyncpg), so no request handler blocks on the database."""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .models import Base

engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=20, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, closed when the request ends."""
    async with SessionLocal() as session:
        yield session


async def create_tables() -> None:
    """Create missing tables at start-up. There are no migrations; see the design document's trade-offs."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
