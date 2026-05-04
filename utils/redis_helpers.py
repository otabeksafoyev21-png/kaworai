# utils/redis_helpers.py  (yangilangan, environs siz)

import os

import redis.asyncio as redis
from dotenv import load_dotenv  # agar python-dotenv o'rnatilgan bo'lsa

# .env ni yuklash (agar loyihada allaqachon config.py da yuklanmagan bo'lsa)
load_dotenv()  # bu .env faylini o'qiydi

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


async def set_watching(user_id: int, anime_id: str, episode: int):
    key = f"watching:{user_id}"
    await redis_client.hset(key, mapping={anime_id: str(episode)})
    await redis_client.expire(key, 86400 * 7)  # 7 kun


async def get_watching(user_id: int, anime_id: str) -> int | None:
    key = f"watching:{user_id}"
    value = await redis_client.hget(key, anime_id)
    return int(value) if value else None


async def clear_watching(user_id: int):
    key = f"watching:{user_id}"
    await redis_client.delete(key)
