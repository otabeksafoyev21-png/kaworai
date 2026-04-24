# middlewares/throttling.py
import os
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from cachetools import TTLCache

from utils.security import parse_admin_ids

# Adminlar throttling dan xoli — aks holda admin panelda tugmalarni tez
# bosganda xabarlar jim o'chib ketadi va buyruqlar ishlamaganday ko'rinadi.
_ADMINS = set(parse_admin_ids(os.getenv("ADMIN_ID", "")))


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate: float = 0.7):
        self.rate = rate
        self.cache = TTLCache(maxsize=10_000, ttl=300)  # 5 daqiqa xotira

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # Adminlar throttling dan xoli — admin panel tugmalarini tez bosganda
        # buyruqlar jim o'tkazib yuborilmasin.
        if str(user.id) in _ADMINS:
            return await handler(event, data)

        key = f"throttle_{user.id}"
        now = time.time()

        if key in self.cache and now - self.cache[key] < self.rate:
            # Oddiy userlar uchun — spam/brute-force'dan himoya.
            return

        self.cache[key] = now
        return await handler(event, data)
