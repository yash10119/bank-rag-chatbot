"""Thin async wrapper over the arq (Redis) broker."""
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from .config import settings

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def enqueue(function: str, *args, **kwargs) -> None:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    await _pool.enqueue_job(function, *args, **kwargs)
