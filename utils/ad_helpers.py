"""
Reklama banner yordamchisi — oddiy (non-Pro) foydalanuvchilarga
video caption ostiga reklama qo'shadi.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Kesh — DB'ga har safar murojaat qilmaslik uchun
_ad_cached_text: str | None = None
_ad_cached_ts: float = 0.0
_AD_TTL = 120.0  # soniya


async def get_ad_text() -> str | None:
    """Faol reklamadan bitta banner matnini qaytaradi (keshli).

    Qaytaradi:
        Reklama matni yoki None (reklama yo'q bo'lsa).
    """
    global _ad_cached_text, _ad_cached_ts
    now = time.monotonic()
    if _ad_cached_text is not None and (now - _ad_cached_ts) < _AD_TTL:
        return _ad_cached_text

    try:
        from database.engine import AsyncSessionLocal
        from database.queries import get_random_active_ad

        async with AsyncSessionLocal() as session:
            ad = await get_random_active_ad(session)
            if ad is None:
                _ad_cached_text = None
                _ad_cached_ts = now
                return None
            if ad.url:
                text = f'\n\n📢 <a href="{ad.url}">{ad.text}</a>'
            else:
                text = f"\n\n📢 {ad.text}"
            _ad_cached_text = text
            _ad_cached_ts = now
            return text
    except Exception:
        logger.debug("get_ad_text: failed", exc_info=True)
        return None


def invalidate_ad_cache() -> None:
    """Admin reklama qo'shganda/o'chirganda chaqiriladi."""
    global _ad_cached_text, _ad_cached_ts
    _ad_cached_text = None
    _ad_cached_ts = 0.0


# Pro foydalanuvchilar uchun reklama yozuvi keshi
_pro_ad_cached_text: str | None = None
_pro_ad_cached_ts: float = 0.0
_PRO_AD_TTL = 120.0


async def get_pro_ad_text() -> str | None:
    """Admin kiritgan Pro reklama yozuvini qaytaradi (keshli)."""
    global _pro_ad_cached_text, _pro_ad_cached_ts
    now = time.monotonic()
    if _pro_ad_cached_text is not None and (now - _pro_ad_cached_ts) < _PRO_AD_TTL:
        return _pro_ad_cached_text if _pro_ad_cached_text else None

    try:
        from database.engine import AsyncSessionLocal
        from database.queries import get_bot_setting

        async with AsyncSessionLocal() as session:
            val = await get_bot_setting(session, "pro_ad_text")
            if not val:
                _pro_ad_cached_text = ""
                _pro_ad_cached_ts = now
                return None
            _pro_ad_cached_text = val
            _pro_ad_cached_ts = now
            return val
    except Exception:
        logger.debug("get_pro_ad_text: failed", exc_info=True)
        return None


def invalidate_pro_ad_cache() -> None:
    """Admin Pro yozuvini o'zgartirganda chaqiriladi."""
    global _pro_ad_cached_text, _pro_ad_cached_ts
    _pro_ad_cached_text = None
    _pro_ad_cached_ts = 0.0
