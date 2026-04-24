"""
Kaworai Pro — Redis Cache Helpers
Mavjud utils/redis_helpers.py ga QO'SHILADI.

Qimmat DB so'rovlarni cache qiladi.
TTL: recommendation = 5 daqiqa, trending = 10 daqiqa
"""

import json
import os
from typing import Any, Optional

import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


# ─── Cache keys ───────────────────────────────────────────
def _rec_key(user_id: int) -> str:
    return f"rec:{user_id}"


def _trending_key(content_type: str = "all") -> str:
    return f"trending:{content_type}"


def _top_key(content_type: str = "all") -> str:
    return f"top:{content_type}"


def _rising_key(content_type: str = "all") -> str:
    return f"rising:{content_type}"


def _hidden_key(content_type: str = "all") -> str:
    return f"hidden:{content_type}"


def _taste_key(user_id: int) -> str:
    return f"taste:{user_id}"


def _related_key(anime_id: int) -> str:
    return f"related:{anime_id}"


# ─── Generic get/set ──────────────────────────────────────


async def cache_get(key: str) -> Optional[Any]:
    try:
        r = await get_redis()
        val = await r.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None


async def cache_set(key: str, value: Any, ttl: int = 300) -> None:
    try:
        r = await get_redis()
        await r.setex(key, ttl, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


async def cache_delete(key: str) -> None:
    try:
        r = await get_redis()
        await r.delete(key)
    except Exception:
        pass


async def cache_delete_pattern(pattern: str) -> None:
    """Masalan: cache_delete_pattern('rec:*') — barcha rec: kalitlarini o'chiradi."""
    try:
        r = await get_redis()
        keys = await r.keys(pattern)
        if keys:
            await r.delete(*keys)
    except Exception:
        pass


# ─── Pro-specific cache funksiyalari ─────────────────────


async def get_cached_recommendations(user_id: int) -> Optional[list]:
    return await cache_get(_rec_key(user_id))


async def set_cached_recommendations(user_id: int, items: list) -> None:
    await cache_set(_rec_key(user_id), items, ttl=300)  # 5 daqiqa


async def invalidate_user_cache(user_id: int) -> None:
    """User yangi kontent ko'rganda uning recommendation cacheni tozalash."""
    await cache_delete(_rec_key(user_id))
    await cache_delete(_taste_key(user_id))


async def get_cached_trending(content_type: str = "all") -> Optional[list]:
    return await cache_get(_trending_key(content_type))


async def set_cached_trending(items: list, content_type: str = "all") -> None:
    await cache_set(_trending_key(content_type), items, ttl=600)  # 10 daqiqa


async def get_cached_top(content_type: str = "all") -> Optional[list]:
    return await cache_get(_top_key(content_type))


async def set_cached_top(items: list, content_type: str = "all") -> None:
    await cache_set(_top_key(content_type), items, ttl=1800)  # 30 daqiqa


async def get_cached_rising(content_type: str = "all") -> Optional[list]:
    return await cache_get(_rising_key(content_type))


async def set_cached_rising(items: list, content_type: str = "all") -> None:
    await cache_set(_rising_key(content_type), items, ttl=300)


async def get_cached_hidden(content_type: str = "all") -> Optional[list]:
    return await cache_get(_hidden_key(content_type))


async def set_cached_hidden(items: list, content_type: str = "all") -> None:
    await cache_set(_hidden_key(content_type), items, ttl=3600)  # 1 soat


async def get_cached_related(anime_id: int) -> Optional[list]:
    return await cache_get(_related_key(anime_id))


async def set_cached_related(anime_id: int, items: list) -> None:
    await cache_set(_related_key(anime_id), items, ttl=3600)
