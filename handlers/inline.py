"""
inline.py — to'liq tuzatilgan

Inline tanlanganda:
  - Foydalanuvchi chatda https://t.me/bot?start=anime_ID linkini yuboradi
  - Bot shu linkni ko'rib anime kartasini chiqaradi (users.py da)

Qidiruv:
  - Bo'sh → Top 18 (9 ko'p ko'rilgan + 9 yuqori reyting)
  - Matn → shu nomdagi animalar

Filtr:
  - Oddiy user: pro_locked animelar ko'rinmaydi
  - Pro user: hammasi ko'rinadi
"""

import os
from datetime import datetime

from aiogram import Router, types
from aiogram.types import (
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from sqlalchemy import select

from database.engine import AsyncSessionLocal
from database.models import Anime, User

inline_router = Router()

BOT_USERNAME = os.getenv("BOT_USERNAME", "kaworai_uz_bot")
DEFAULT_THUMB = "https://i.imgur.com/JyOSMOR.png"


async def _is_pro(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user or not user.is_pro:
            return False
        if user.pro_until and user.pro_until < datetime.utcnow():
            user.is_pro = False
            user.pro_until = None
            await session.commit()
            return False
        return True


def _make_result(anime: Anime) -> InlineQueryResultArticle:
    """
    Inline natija — tanlanganda foydalanuvchi chatda
    https://t.me/BOT?start=anime_ID linkini yuboradi.
    users.py dagi handle_text yoki cmd_start bu linkni ushlaydi
    va anime kartasini ko'rsatadi.
    """
    genres_text = ", ".join((anime.genres or [])[:3]) or "Nomalum"
    ep_text = str(getattr(anime, "episodes_count", None) or anime.total_episodes or "?")
    thumb = anime.inline_thumbnail_url or DEFAULT_THUMB
    lock_icon = "🔒 " if getattr(anime, "is_pro_locked", False) else ""

    share_url = f"https://t.me/{BOT_USERNAME}?start=anime_{anime.id}"

    desc = f"{lock_icon}⭐ {anime.rating:.1f}  📅 {anime.year or '—'}  🎭 {genres_text[:25]}  📺 {ep_text} qism"

    return InlineQueryResultArticle(
        id=str(anime.id),
        title=f"🎬 {lock_icon}{anime.title}",
        description=desc,
        thumbnail_url=thumb,
        # Foydalanuvchi tanlasa — shu link chatga yuboriladi
        # Bot handle_text orqali link kelganda anime kartasini yuboradi
        input_message_content=InputTextMessageContent(
            message_text=share_url,
        ),
    )


@inline_router.inline_query()
async def query_anime(query: types.InlineQuery):
    search_text = query.query.strip()
    user_id = query.from_user.id
    is_pro = await _is_pro(user_id)

    async with AsyncSessionLocal() as session:
        if not search_text:
            # Bo'sh qidiruv → Top 18
            top_views = (await session.execute(select(Anime).order_by(Anime.views.desc()).limit(9))).scalars().all()

            top_rated = (
                (
                    await session.execute(
                        select(Anime).where(Anime.rating_count >= 1).order_by(Anime.rating.desc()).limit(9)
                    )
                )
                .scalars()
                .all()
            )

            seen = set()
            animes = []
            for a in list(top_views) + list(top_rated):
                if a.id not in seen:
                    seen.add(a.id)
                    animes.append(a)
        else:
            result = await session.execute(select(Anime).where(Anime.title.ilike(f"%{search_text}%")).limit(30))
            animes = result.scalars().all()

    results = []
    for anime in animes:
        # Oddiy user: pro_locked ko'rinmaydi
        if not is_pro and getattr(anime, "is_pro_locked", False):
            continue
        results.append(_make_result(anime))

    await query.answer(results, cache_time=5, is_personal=True)
