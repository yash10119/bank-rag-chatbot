"""Thin async wrapper over the arq (Redis) broker."""
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from .config import settings

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    """arq connection settings derived from REDIS_URL."""
    return RedisSettings.from_dsn(settings.redis_url)


async def enqueue(function: str, *args, **kwargs) -> None:
    """Queue a background job by function name (ingestion, resume after approval, audit post). The pool is created lazily."""
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    await _pool.enqueue_job(function, *args, **kwargs)
