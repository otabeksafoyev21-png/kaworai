import json
from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from database.engine import AsyncSessionLocal
from database.models import Anime, User

genre_router = Router()

GENRES = {
    "Jang": "⚔️ Action",
    "Sarguzasht": "🗺️ Adventure",
    "Komediya": "😂 Comedy",
    "Drama": "🎭 Drama",
    "Fantaziya": "🧙 Fantasy",
    "Qo'rqinchli": "👻 Horror",
    "Sirli": "🔍 Mystery",
    "Romantika": "❤️ Romance",
    "Ilmiy fantastika": "🚀 SciFi",
    "Oddiy hayot": "☕ SliceOfLife",
    "Sport": "⚽ Sports",
    "G'ayritabiiy": "✨ Supernatural",
    "Triller": "😱 Thriller",
    "Mexanik": "🤖 Mecha",
    "Sehr": "🪄 Magic",
    "Maktab": "🏫 School",
    "Shonen": "👦 Shounen",
    "Shojo": "👧 Shoujo",
    "Isekai": "🌀 Isekai",
    "Psixologik": "🧠 Psychological",
}

# Aliases — har qanday tilda/formatda yozilgan janrni GENRES kalitiga
# (o'zbekcha kanonik nom, masalan "Jang") aylantirish uchun xarita.
# Eski ma'lumotlarda ingliz tilidagi yozuvlar ham bor, shuning uchun
# ikkala til ham bitta kalitga yig'iladi.
GENRE_ALIASES = {
    # Inglizcha → o'zbekcha kanonik
    "action": "Jang",
    "adventure": "Sarguzasht",
    "comedy": "Komediya",
    "drama": "Drama",
    "fantasy": "Fantaziya",
    "horror": "Qo'rqinchli",
    "mystery": "Sirli",
    "romance": "Romantika",
    "sci-fi": "Ilmiy fantastika",
    "scifi": "Ilmiy fantastika",
    "sci fi": "Ilmiy fantastika",
    "slice of life": "Oddiy hayot",
    "sliceoflife": "Oddiy hayot",
    "sports": "Sport",
    "sport": "Sport",
    "supernatural": "G'ayritabiiy",
    "thriller": "Triller",
    "mecha": "Mexanik",
    "magic": "Sehr",
    "school": "Maktab",
    "shounen": "Shonen",
    "shonen": "Shonen",
    "shoujo": "Shojo",
    "shojo": "Shojo",
    "isekai": "Isekai",
    "psychological": "Psixologik",
}

GENRE_PAGE_SIZE = 8
ANIME_PAGE_SIZE = 6


def normalize_genre(g: str) -> str:
    """
    Janr qatorini kanonik GENRES kalitiga aylantiradi.

    1) Agar aynan GENRES kalitlaridan biri bo'lsa — shu holicha qoladi.
    2) Agar GENRE_ALIASES da bo'lsa — shunga mos kanonik kalit qaytariladi.
    3) Aks holda kirishning o'zi qaytariladi (qidirishda mos kelmaydi,
       lekin ma'lumotlar buzilmaydi).
    """
    if not g:
        return ""
    g = g.strip()
    if g in GENRES:
        return g
    lower = g.lower()
    if lower in GENRE_ALIASES:
        return GENRE_ALIASES[lower]
    # emoji + matn formati bo'lsa ("⚔️ Action") — emoji olib qarab ko'ramiz
    for key, label in GENRES.items():
        if label.lower() == lower or key.lower() == lower:
            return key
    return g


def parse_genres(raw_genres) -> list:
    if raw_genres is None:
        return []
    if isinstance(raw_genres, list):
        return raw_genres
    if isinstance(raw_genres, str):
        raw_genres = raw_genres.strip()
        try:
            parsed = json.loads(raw_genres)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [g.strip() for g in raw_genres.split(",") if g.strip()]
    return []


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


def genres_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    keys = list(GENRES.keys())
    total = (len(keys) - 1) // GENRE_PAGE_SIZE
    start = page * GENRE_PAGE_SIZE
    page_keys = keys[start : start + GENRE_PAGE_SIZE]

    buttons = []
    row = []
    for key in page_keys:
        # Janr tanlash tugmalari — Bot API 9.4 success (yashil).
        row.append(InlineKeyboardButton(text=GENRES[key], callback_data=f"gshow:{key}:{page}", style="success"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"gpage:{page - 1}", style="primary"))
    if page < total:
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"gpage:{page + 1}", style="primary"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="🏠 Asosiy menyu", callback_data="main_menu", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def anime_list_keyboard(animes: list, genre_key: str, from_page: int, page: int = 0) -> InlineKeyboardMarkup:
    total_pages = (len(animes) - 1) // ANIME_PAGE_SIZE if animes else 0
    start = page * ANIME_PAGE_SIZE
    page_animes = animes[start : start + ANIME_PAGE_SIZE]

    buttons = []
    for anime in page_animes:
        lock = "🔒 " if anime.is_pro_locked else ""
        # Pro-locked kontent — ko'k (primary), oddiy — yashil (success).
        style = "primary" if anime.is_pro_locked else "success"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🎬 {lock}{anime.title}",
                    callback_data=f"anime_info_{anime.id}",
                    style=style,
                )
            ]
        )

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Oldingi",
                callback_data=f"glist:{genre_key}:{from_page}:{page - 1}",
                style="primary",
            )
        )
    if page < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"glist:{genre_key}:{from_page}:{page + 1}",
                style="primary",
            )
        )
    if nav:
        buttons.append(nav)

    buttons.append(
        [InlineKeyboardButton(text="🔙 Janrlarga qaytish", callback_data=f"gpage:{from_page}", style="primary")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def get_animes_by_genre(genre_key: str, is_pro: bool = False) -> list:
    """
    Genre bo'yicha animalarni qaytaradi.
    is_pro=False bo'lsa — pro-locked animelar CHIQMAYDI.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Anime))
        all_animes = result.scalars().all()

        matched = []
        for anime in all_animes:
            # ── Pro-locked filter ──
            if anime.is_pro_locked and not is_pro:
                continue

            genres_list = parse_genres(anime.genres)
            for g in genres_list:
                if normalize_genre(g) == genre_key:
                    matched.append(anime)
                    break
        return matched


# ── JANRLAR RO'YXATI ────────────────────────────────────────


@genre_router.callback_query(F.data == "genres")
async def show_genres(call: CallbackQuery):
    kb = genres_keyboard(page=0)
    try:
        await call.message.edit_caption(caption="🎭 <b>Janr tanlang:</b>", reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            await call.message.edit_text(text="🎭 <b>Janr tanlang:</b>", reply_markup=kb, parse_mode="HTML")
        except Exception:
            await call.message.answer("🎭 <b>Janr tanlang:</b>", reply_markup=kb, parse_mode="HTML")
    await call.answer()


@genre_router.callback_query(F.data.startswith("gpage:"))
async def genre_page(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    kb = genres_keyboard(page=page)
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        try:
            await call.message.edit_caption(caption="🎭 <b>Janr tanlang:</b>", reply_markup=kb, parse_mode="HTML")
        except Exception:
            await call.message.answer("🎭 <b>Janr tanlang:</b>", reply_markup=kb, parse_mode="HTML")
    await call.answer()


@genre_router.callback_query(F.data.startswith("gshow:"))
async def show_genre_animes(call: CallbackQuery):
    parts = call.data.split(":")
    genre_key = parts[1]
    from_page = int(parts[2])
    user_id = call.from_user.id

    is_pro = await _is_pro(user_id)
    matched = await get_animes_by_genre(genre_key, is_pro=is_pro)
    genre_uz = GENRES.get(genre_key, genre_key)

    if not matched:
        await call.answer(f"😔 {genre_uz} janrida anime topilmadi!", show_alert=True)
        return

    kb = anime_list_keyboard(matched, genre_key, from_page, page=0)
    text = f"🎭 <b>{genre_uz}</b> — {len(matched)} ta anime:"

    try:
        await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            await call.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@genre_router.callback_query(F.data.startswith("glist:"))
async def genre_anime_page(call: CallbackQuery):
    parts = call.data.split(":")
    genre_key = parts[1]
    from_page = int(parts[2])
    page = int(parts[3])
    user_id = call.from_user.id

    is_pro = await _is_pro(user_id)
    matched = await get_animes_by_genre(genre_key, is_pro=is_pro)
    genre_uz = GENRES.get(genre_key, genre_key)

    kb = anime_list_keyboard(matched, genre_key, from_page, page=page)
    text = f"🎭 <b>{genre_uz}</b> — {len(matched)} ta anime:"

    try:
        await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            await call.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()
