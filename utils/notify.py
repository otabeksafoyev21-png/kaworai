"""
utils/notify.py

Yangi qism qo'shilganda animega obuna bo'lgan
barcha foydalanuvchilarga xabar yuboradi.

FOYDALANISH:
    admin.py dagi add_episode_from_channel handlerga:
    from utils.notify import notify_new_episode
    await notify_new_episode(bot, anime_id, episode_number)
"""

import asyncio
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.engine import AsyncSessionLocal
from database.models import Anime
from database.queries import get_anime_subscribers

logger = logging.getLogger(__name__)


async def notify_new_episode(
    bot: Bot,
    anime_id: int,
    episode_number: int,
) -> dict:
    """
    Animega obuna bo'lgan userlarga yangi qism xabari yuboradi.
    Returns: {"sent": int, "failed": int}
    """
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        subscriber_ids = await get_anime_subscribers(session, anime_id)

    if not anime or not subscriber_ids:
        return {"sent": 0, "failed": 0}

    import os

    BOT_USERNAME = os.getenv("BOT_USERNAME", "kaworai_uz_bot")

    text = (
        f"🔔 <b>Yangi qism chiqdi!</b>\n\n"
        f"🎬 <b>{anime.title}</b>\n"
        f"▶️ {episode_number}-qism\n\n"
        f"Ko'rish uchun quyidagi tugmani bosing:"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"▶️ {episode_number}-qismni ko'rish", url=f"https://t.me/{BOT_USERNAME}?start=anime_{anime_id}"
                )
            ]
        ]
    )

    # Poster bilan yoki matnsiz
    sent = 0
    failed = 0

    for user_id in subscriber_ids:
        try:
            if anime.poster_file_id:
                await bot.send_photo(
                    chat_id=user_id, photo=anime.poster_file_id, caption=text, reply_markup=kb, parse_mode="HTML"
                )
            else:
                await bot.send_message(chat_id=user_id, text=text, reply_markup=kb, parse_mode="HTML")
            sent += 1
        except Exception as e:
            err = str(e).lower()
            # Bot blocked yoki user yo'q — obunani o'chirish
            if any(x in err for x in ("blocked", "not found", "deactivated", "chat not found")):
                try:
                    async with AsyncSessionLocal() as session:
                        from database.queries import unsubscribe_anime

                        await unsubscribe_anime(session, anime_id, user_id)
                except Exception:
                    pass
            failed += 1

        # Flood limit — har 25 ta xabarda 1 soniya kutish
        if (sent + failed) % 25 == 0:
            await asyncio.sleep(1)

    logger.info(f"Notify: {anime.title} {episode_number}-qism — yuborildi: {sent}, xato: {failed}")
    return {"sent": sent, "failed": failed}
