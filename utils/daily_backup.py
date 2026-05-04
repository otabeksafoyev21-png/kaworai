"""
Davriy ZIP zaxira scheduler'i.

Har N soatda (standart — 48 soat, ya'ni 2 kunda bir marta) animelar +
kanallar jadvallarini JSON shaklida olib, ZIP qilib `BACKUP_CHANNEL_ID`
kanaliga yuboradi. Agar `BACKUP_CHANNEL_ID` 0 bo'lsa — scheduler ishga
tushmaydi (o'chiq). Interval `BACKUP_INTERVAL_HOURS` env bilan o'zgaradi.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import io
import json
import logging
import os
import zipfile

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select

from data.config import BACKUP_CHANNEL_ID
from database.engine import AsyncSessionLocal
from database.models import Anime, SubscriptionChannel

logger = logging.getLogger(__name__)


def _resolve_interval_seconds() -> int:
    """BACKUP_INTERVAL_HOURS env'dan masofani oladi (standart 48 soat)."""
    raw = (os.getenv("BACKUP_INTERVAL_HOURS", "") or "").strip()
    try:
        hours = float(raw) if raw else 48.0
    except ValueError:
        hours = 48.0
    # Eng kamida 1 soat — juda kichik qiymatlar spam bo'lib ketmasin.
    hours = max(hours, 1.0)
    return int(hours * 60 * 60)


# Ikki yuborish orasidagi masofa (soniya). Standart — 48 soat (2 kun).
DAILY_BACKUP_INTERVAL = _resolve_interval_seconds()

BACKUP_VERSION = 1


def _anime_to_dict(a: Anime) -> dict:
    """Anime jadval yozuvini JSON-serializable dict ga aylantiradi."""
    return {
        "id": a.id,
        "title": a.title,
        "description": a.description,
        "poster_file_id": a.poster_file_id,
        "trailer_file_id": a.trailer_file_id,
        "inline_thumbnail_url": a.inline_thumbnail_url,
        "genres": list(a.genres) if a.genres else [],
        "year": a.year,
        "rating": a.rating,
        "rating_count": a.rating_count,
        "total_episodes": a.total_episodes,
        "views": a.views,
        "content_type": a.content_type,
        "tags": list(a.tags) if a.tags else [],
        "mood": list(a.mood) if a.mood else [],
        "episodes_count": a.episodes_count,
        "duration": a.duration,
        "status": a.status,
        "popularity": a.popularity,
        "popularity_score": a.popularity_score,
        "is_hidden_gem": a.is_hidden_gem,
        "is_pro_locked": a.is_pro_locked,
    }


def _channel_to_dict(c: SubscriptionChannel) -> dict:
    return {
        "id": c.id,
        "channel_id": c.channel_id,
        "username": c.username,
        "channel_url": c.channel_url,
        "channel_name": c.channel_name,
        "is_active": c.is_active,
        "require_check": c.require_check,
        "is_news": c.is_news,
    }


async def _build_backup_zip() -> tuple[bytes, str, int, int]:
    """
    Bazadan barcha anime va kanallarni o'qib, ZIP bytes qaytaradi.

    Qaytadi: (zip_bytes, filename, anime_count, channel_count)
    """
    async with AsyncSessionLocal() as session:
        animes = (await session.execute(select(Anime))).scalars().all()
        channels = (await session.execute(select(SubscriptionChannel))).scalars().all()

    metadata = {
        "version": BACKUP_VERSION,
        "exported_at": _dt.datetime.utcnow().isoformat() + "Z",
        "filter": "daily-auto",
        "counts": {"animes": len(animes), "channels": len(channels)},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "animes.json",
            json.dumps([_anime_to_dict(a) for a in animes], ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "channels.json",
            json.dumps([_channel_to_dict(c) for c in channels], ensure_ascii=False, indent=2),
        )
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    buf.seek(0)
    fname = f"kaworai_daily_backup_{_dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.zip"
    return buf.getvalue(), fname, len(animes), len(channels)


async def send_daily_backup(bot: Bot) -> bool:
    """Bir martagina zaxira yig'ib backup kanaliga yuboradi."""
    if not BACKUP_CHANNEL_ID:
        logger.debug("daily backup: BACKUP_CHANNEL_ID o'rnatilmagan, tashlab ketildi")
        return False
    try:
        data, fname, n_anime, n_ch = await _build_backup_zip()
    except Exception:
        logger.exception("daily backup: ZIP yig'ishda xato")
        return False
    try:
        await bot.send_document(
            chat_id=BACKUP_CHANNEL_ID,
            document=BufferedInputFile(data, filename=fname),
            caption=(
                "🗄 <b>Kunlik avtomatik zaxira</b>\n"
                f"🎬 Anime: {n_anime}\n"
                f"📢 Kanal: {n_ch}\n"
                f"🕒 {_dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            ),
            parse_mode="HTML",
        )
        logger.info("daily backup: yuborildi (%s anime, %s kanal)", n_anime, n_ch)
        return True
    except Exception:
        logger.exception("daily backup: kanalga yuborishda xato (id=%s)", BACKUP_CHANNEL_ID)
        return False


async def daily_backup_loop(bot: Bot, interval: int = DAILY_BACKUP_INTERVAL) -> None:
    """Cheksiz loop: har `interval` soniyada zaxira yuboradi."""
    if not BACKUP_CHANNEL_ID:
        logger.info("daily backup: BACKUP_CHANNEL_ID=0 — scheduler o'chiq")
        return
    logger.info(
        "daily backup: scheduler ishga tushdi (kanal=%s, interval=%ss)",
        BACKUP_CHANNEL_ID,
        interval,
    )
    # Ishga tushganda darrov yubormaymiz — bir necha daqiqa kutib, keyin sikl.
    await asyncio.sleep(60)
    while True:
        await send_daily_backup(bot)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("daily backup: scheduler to'xtatildi")
            raise
