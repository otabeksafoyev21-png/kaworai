import time

from aiogram.fsm.storage.redis import RedisStorage

from config import config

redis = RedisStorage.from_url(config.REDIS_URL).redis


async def set_watching(user_id: int, anime_id: int, episode: int = 1):
    await redis.hset(
        f"watch:{user_id}",
        mapping={"anime_id": str(anime_id), "episode": str(episode), "timestamp": str(int(time.time()))},
    )


async def get_watching(user_id: int):
    data = await redis.hgetall(f"watch:{user_id}")
    if not data:
        return None
    return {k.decode(): v.decode() for k, v in data.items()}


async def clear_watching(user_id: int):
    await redis.delete(f"watch:{user_id}")
