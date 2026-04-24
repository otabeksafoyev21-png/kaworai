import os
from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from sqlalchemy import func, select

from database.engine import AsyncSessionLocal
from database.models import Anime, Series, User
from database.queries import (
    add_or_update_rating,
    add_to_watch_history,
    get_user_rating,
    is_subscribed_anime,
    record_view,
    subscribe_anime,
    unsubscribe_anime,
)

callback_router = Router()
BOT_USERNAME = os.getenv("BOT_USERNAME", "kaworai_uz_bot")

EP_PAGE_SIZE = 12  # 4 ta × 3 qator


# ── Pro tekshirish ───────────────────────────────────────────
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


# ── Caption ──────────────────────────────────────────────────
def _anime_caption(anime: Anime) -> str:
    type_emoji = {"anime": "🎌", "movie": "🎥", "serial": "📺", "dorama": "🌸"}
    emoji = type_emoji.get(getattr(anime, "content_type", None) or "anime", "🎬")
    genres_text = ", ".join(anime.genres or []) or "Nomalum"
    tags = getattr(anime, "tags", None) or []
    tags_text = ", ".join(tags[:3])
    lock_str = " 🔒 Pro" if getattr(anime, "is_pro_locked", False) else ""
    status_map = {
        "completed": "✅ Tugagan",
        "ongoing": "📡 Davom etmoqda",
        "announced": "📢 Kutilmoqda",
    }
    status_str = status_map.get(getattr(anime, "status", "") or "", "")

    return (
        f"{emoji} <b>{anime.title}</b>" + (f" ({anime.year})" if anime.year else "") + lock_str + "\n\n"
        f"🎭 {genres_text}\n"
        + (f"🏷 {tags_text}\n" if tags_text else "")
        + f"⭐ {anime.rating:.1f} ({anime.rating_count} ovoz)\n"
        + (f"📊 {status_str}\n" if status_str else "")
        + f"🆔 Kod: <code>{anime.id}</code>\n\n"
        f"📖 {(anime.description or '')[:300]}"
    )


# ── Anime info keyboard ──────────────────────────────────────
def _anime_info_kb(
    anime_id: int,
    has_episodes: bool,
    subscribed: bool,
    is_pro: bool,
    is_pro_locked: bool,
) -> InlineKeyboardMarkup:
    # Bot API 9.4 ranglari: success (yashil) = tomosha/boshlash, primary (ko'k) = menyu/ulashish,
    # danger (qizil) = bekor qilish/obunani uzish.
    rows = []

    if is_pro_locked and not is_pro:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔒 Faqat Kaworai Pro uchun",
                    callback_data="kawaii_pass",
                    style="primary",
                )
            ]
        )
    elif has_episodes:
        rows.append(
            [
                InlineKeyboardButton(
                    text="▶️ 1-qismdan tomosha qilish",
                    callback_data=f"watch_start_{anime_id}",
                    style="success",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="📋 Barcha qismlar",
                    callback_data=f"episodes_{anime_id}_0",
                    style="primary",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⏳ Qismlar hali qo'shilmagan",
                    callback_data="no_episodes",
                    style="primary",
                )
            ]
        )

    sub_icon = "🔔" if subscribed else "🔕"
    sub_txt = "Obunani bekor qilish" if subscribed else "Obuna bo'lish"
    sub_style = "danger" if subscribed else "success"
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{sub_icon} {sub_txt}",
                callback_data=f"toggle_sub_{anime_id}",
                style=sub_style,
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔗 Ulashish",
                url=f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}%3Fstart%3Danime_{anime_id}",
                style="primary",
            )
        ]
    )

    rows.append([InlineKeyboardButton(text="🏠 Asosiy menyu", callback_data="main_menu", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Player keyboard ──────────────────────────────────────────
def _player_kb(
    anime_id: int,
    episode: int,
    total_eps: int,
    max_ep: int,
    is_last: bool,
    user_rated: bool,
    subscribed: bool,
    is_pro: bool,
) -> InlineKeyboardMarkup:
    rows = []

    # Navigatsiya — primary (ko'k) = qism ko'chirish.
    nav = []
    if episode > 1:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Oldingi",
                callback_data=f"ep_{anime_id}_{episode - 1}",
                style="primary",
            )
        )
    if episode < max_ep:
        nav.append(
            InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"ep_{anime_id}_{episode + 1}",
                style="primary",
            )
        )
    if nav:
        rows.append(nav)

    # Qismlar/Asosiy — primary (ko'k).
    ep_page = max(0, (episode - 1) // EP_PAGE_SIZE)
    rows.append(
        [
            InlineKeyboardButton(
                text="📋 Qismlar",
                callback_data=f"episodes_{anime_id}_{ep_page}",
                style="primary",
            ),
            InlineKeyboardButton(text="🏠 Asosiy", callback_data="main_menu", style="primary"),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="⚠️ Muammo bormi?",
                callback_data=f"problems_{anime_id}_{episode}",
                style="danger",
            )
        ]
    )

    sub_icon = "🔔" if subscribed else "🔕"
    sub_txt = "Obunani bekor qilish" if subscribed else "Obuna bo'lish"
    sub_style = "danger" if subscribed else "success"
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{sub_icon} {sub_txt}",
                callback_data=f"toggle_sub_{anime_id}",
                style=sub_style,
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔗 Ulashish",
                url=f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}%3Fstart%3Danime_{anime_id}",
                style="primary",
            )
        ]
    )

    if is_last and not user_rated:
        rows.append([InlineKeyboardButton(text="⭐ Baho berish", callback_data=f"rate_{anime_id}", style="success")])
    elif is_last and user_rated:
        rows.append([InlineKeyboardButton(text="✅ Baho berilgan", callback_data=f"rated_{anime_id}", style="success")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Qismlar keyboard — 4×3 = 12 ta, sahifalash ──────────────
def _episodes_kb(
    anime_id: int,
    episodes: list,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """
    12 ta qism ko'rsatiladi (4 ta × 3 qator).
    12 tadan ko'p bo'lsa keyingi/oldingi sahifa tugmalari chiqadi.
    """
    total = len(episodes)
    total_pages = max(1, (total + EP_PAGE_SIZE - 1) // EP_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * EP_PAGE_SIZE
    end = start + EP_PAGE_SIZE
    page_eps = sorted(episodes, key=lambda e: e.episode)[start:end]

    # Qism tugmalari — success (yashil), sahifa/orqaga — primary (ko'k).
    rows = []
    row = []
    for ep in page_eps:
        row.append(
            InlineKeyboardButton(
                text=str(ep.episode),
                callback_data=f"ep_{anime_id}_{ep.episode}",
                style="success",
            )
        )
        if len(row) == 4:  # 4 ta × 3 qator = 12 ta
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text=f"⬅️ {page}",
                    callback_data=f"episodes_{anime_id}_{page - 1}",
                    style="primary",
                )
            )
        nav.append(InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="noop", style="primary"))
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton(
                    text=f"{page + 2} ➡️",
                    callback_data=f"episodes_{anime_id}_{page + 1}",
                    style="primary",
                )
            )
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data=f"anime_info_{anime_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Video yuborish yordamchi ─────────────────────────────────
async def _send_or_edit_video(
    call: CallbackQuery,
    ep_file_id: str,
    caption: str,
    kb: InlineKeyboardMarkup,
    is_pro: bool,
) -> None:
    """
    Pro user   → edit_media (xabar o'zgarmaydi, videolar to'planadi)
    Oddiy user → avvalgi o'chirilmaydi, faqat video o'zgaradi (edit_media)
                 Agar xabar video bo'lsa edit ishlaydi, bo'lmasa yangi yuboradi.

    MUHIM: Oddiy user uchun protect_content=True (yuklab olish, forward blok).
    """
    if is_pro:
        # Pro: edit_media — xabar saqlanib qoladi
        try:
            await call.message.edit_media(
                InputMediaVideo(media=ep_file_id, caption=caption, parse_mode="HTML"), reply_markup=kb
            )
            return
        except Exception:
            pass
        # edit ishlamasa (masalan, dastlabki rasm xabari) — yangi yuborish
        await call.message.answer_video(video=ep_file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        # Oddiy user: edit_media orqali VIDEO O'ZGARADI, xabar o'chмайди
        # Agar avvalgi xabar video bo'lsa → edit ishlaydi
        # Agar rasm/matn bo'lsa → yangi video yuboriladi (protect_content)
        try:
            await call.message.edit_media(
                InputMediaVideo(media=ep_file_id, caption=caption, parse_mode="HTML"), reply_markup=kb
            )
            return
        except Exception:
            pass
        # edit ishlamadi (masalan, poster rasm edi) — yangi protect_content video
        await call.message.answer_video(
            video=ep_file_id, caption=caption, reply_markup=kb, parse_mode="HTML", protect_content=True
        )


# ═══════════════════════════════════════════════════════════
#  ANIME INFO
# ═══════════════════════════════════════════════════════════


@callback_router.callback_query(F.data.startswith("anime_info_"))
async def show_anime_info(call: CallbackQuery):
    anime_id = int(call.data.replace("anime_info_", ""))
    user_id = call.from_user.id
    is_pro = await _is_pro(user_id)

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return await call.answer("❌ Anime topilmadi!", show_alert=True)

        if getattr(anime, "is_pro_locked", False) and not is_pro:
            return await call.answer(
                "🔒 Bu kontent faqat Kaworai Pro uchun!\nPro olish uchun 🟢 Kaworai Pro tugmasini bosing.",
                show_alert=True,
            )

        ep_count = (
            await session.execute(select(func.count(Series.id)).where(Series.anime_id == anime_id))
        ).scalar() or 0

        subscribed = await is_subscribed_anime(session, anime_id, user_id)

    caption = _anime_caption(anime)
    kb = _anime_info_kb(anime_id, ep_count > 0, subscribed, is_pro, getattr(anime, "is_pro_locked", False))

    try:
        if anime.poster_file_id:
            await call.message.edit_media(
                InputMediaPhoto(media=anime.poster_file_id, caption=caption, parse_mode="HTML"), reply_markup=kb
            )
        else:
            try:
                await call.message.edit_caption(caption=caption, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await call.message.edit_text(caption, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(caption, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  WATCH START — 1-qismdan boshlash
# ═══════════════════════════════════════════════════════════


@callback_router.callback_query(F.data.startswith("watch_start_"))
async def watch_start(call: CallbackQuery):
    anime_id = int(call.data.replace("watch_start_", ""))
    user_id = call.from_user.id
    is_pro = await _is_pro(user_id)

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return await call.answer("❌ Topilmadi!", show_alert=True)
        if getattr(anime, "is_pro_locked", False) and not is_pro:
            return await call.answer("🔒 Faqat Pro uchun!", show_alert=True)

        result = await session.execute(select(Series).where(Series.anime_id == anime_id).order_by(Series.episode.asc()))
        episodes = result.scalars().all()
        user_rating = await get_user_rating(session, anime_id, user_id)
        subscribed = await is_subscribed_anime(session, anime_id, user_id)

    if not episodes:
        return await call.answer("❌ Hali qismlar qo'shilmagan!", show_alert=True)

    ep = episodes[0]
    total = len(episodes)
    max_ep = max(e.episode for e in episodes)
    is_last = ep.episode == max_ep
    user_rated = user_rating is not None

    kb = _player_kb(anime_id, ep.episode, total, max_ep, is_last, user_rated, subscribed, is_pro)
    caption = f"🎬 <b>{anime.title}</b>\n▶️ {ep.episode}-qism  |  📺 Jami: {total} qism"

    async with AsyncSessionLocal() as session:
        await add_to_watch_history(session, user_id, anime_id, ep.episode)
        await record_view(session, anime_id, user_id)

    await _send_or_edit_video(call, ep.file_id, caption, kb, is_pro)
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  EPISODE — qism navigatsiyasi
# ═══════════════════════════════════════════════════════════


@callback_router.callback_query(F.data.startswith("ep_"))
async def show_episode(call: CallbackQuery):
    parts = call.data.split("_")
    anime_id = int(parts[1])
    ep_str = parts[2]

    if ep_str == "cancel":
        return await call.answer()

    episode = int(ep_str)
    user_id = call.from_user.id
    is_pro = await _is_pro(user_id)

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return await call.answer("❌ Topilmadi!", show_alert=True)
        if getattr(anime, "is_pro_locked", False) and not is_pro:
            return await call.answer("🔒 Faqat Pro uchun!", show_alert=True)

        result = await session.execute(select(Series).where(Series.anime_id == anime_id).order_by(Series.episode.asc()))
        episodes = result.scalars().all()
        user_rating = await get_user_rating(session, anime_id, user_id)
        subscribed = await is_subscribed_anime(session, anime_id, user_id)

    ep = next((e for e in episodes if e.episode == episode), None)
    if not ep:
        return await call.answer("❌ Bu qism topilmadi!", show_alert=True)

    total = len(episodes)
    max_ep = max(e.episode for e in episodes)
    is_last = episode == max_ep
    user_rated = user_rating is not None

    kb = _player_kb(anime_id, episode, total, max_ep, is_last, user_rated, subscribed, is_pro)
    caption = f"🎬 <b>{anime.title}</b>\n▶️ {episode}-qism  |  📺 Jami: {total} qism"

    async with AsyncSessionLocal() as session:
        await add_to_watch_history(session, user_id, anime_id, episode, is_completed=(episode == max_ep))

    await _send_or_edit_video(call, ep.file_id, caption, kb, is_pro)
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  EPISODES LIST — 12 ta, sahifalash bilan
# ═══════════════════════════════════════════════════════════


@callback_router.callback_query(F.data.startswith("episodes_"))
async def show_episodes_list(call: CallbackQuery):
    # Format: episodes_{anime_id}_{page}
    parts = call.data.split("_")
    anime_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    is_pro = await _is_pro(call.from_user.id)

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return await call.answer("❌ Topilmadi!", show_alert=True)
        if getattr(anime, "is_pro_locked", False) and not is_pro:
            return await call.answer("🔒 Faqat Pro uchun!", show_alert=True)

        result = await session.execute(select(Series).where(Series.anime_id == anime_id).order_by(Series.episode.asc()))
        episodes = result.scalars().all()

    if not episodes:
        return await call.answer("❌ Qismlar yo'q!", show_alert=True)

    total = len(episodes)
    total_pages = max(1, (total + EP_PAGE_SIZE - 1) // EP_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    kb = _episodes_kb(anime_id, episodes, page)
    text = (
        f"🎬 <b>{anime.title}</b>\n"
        f"📺 Jami {total} qism"
        + (f"  |  📄 {page + 1}/{total_pages}-sahifa" if total_pages > 1 else "")
        + "\n\nQaysi qismdan tomosha qilmoqchisiz?"
    )

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# noop — sahifa ko'rsatkichi uchun
@callback_router.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery):
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  OBUNA TOGGLE
# ═══════════════════════════════════════════════════════════


@callback_router.callback_query(F.data.startswith("toggle_sub_"))
async def toggle_subscription(call: CallbackQuery):
    anime_id = int(call.data.replace("toggle_sub_", ""))
    user_id = call.from_user.id

    async with AsyncSessionLocal() as session:
        already = await is_subscribed_anime(session, anime_id, user_id)
        if already:
            await unsubscribe_anime(session, anime_id, user_id)
            await call.answer("🔕 Obuna bekor qilindi!", show_alert=True)
        else:
            await subscribe_anime(session, anime_id, user_id)
            await call.answer("🔔 Obuna bo'ldingiz!\nYangi qismlar chiqsa xabar beramiz.", show_alert=True)

    # Anime info sahifasini yangilash
    await show_anime_info(
        CallbackQuery(
            id=call.id,
            from_user=call.from_user,
            message=call.message,
            data=f"anime_info_{anime_id}",
            chat_instance=call.chat_instance,
        )
    )


# ═══════════════════════════════════════════════════════════
#  MUAMMOLAR
# ═══════════════════════════════════════════════════════════


@callback_router.callback_query(F.data.startswith("problems_"))
async def show_problems_menu(call: CallbackQuery):
    parts = call.data.split("_")
    anime_id = parts[1]
    episode = parts[2]

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔊 Ovoz tezlashib ketgan", callback_data=f"prob_speed_{anime_id}_{episode}", style="primary"
                )
            ],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data=f"ep_{anime_id}_{episode}", style="primary")],
        ]
    )
    try:
        await call.message.edit_caption(
            caption="⚠️ <b>Epizodda muammo bormi?</b>\n\nPastdagi menyudan tanlang:", reply_markup=kb, parse_mode="HTML"
        )
    except Exception:
        await call.message.answer(
            "⚠️ <b>Epizodda muammo bormi?</b>\n\nPastdagi menyudan tanlang:", reply_markup=kb, parse_mode="HTML"
        )
    await call.answer()


@callback_router.callback_query(F.data.startswith("prob_speed_"))
async def problem_speed(call: CallbackQuery):
    parts = call.data.split("_")
    anime_id = parts[2]
    episode = parts[3]
    is_pro = await _is_pro(call.from_user.id)

    text = (
        "🔊 <b>Ovoz tezlashib ketgan — yechim:</b>\n\n"
        "1️⃣ Telegramning <b>keshini tozalang:</b>\n"
        "   <i>Sozlamalar → Ma'lumotlar va saqlash → Keshni tozalash</i>\n\n"
        "2️⃣ Agar hal bo'lmasa, epizodni qurilmangizning "
        "<b>gallereyasiga saqlang</b> va o'sha yerdan tomosha qiling.\n\n"
        "✅ Bu 2 usul 90% holatlarda muammoni hal qiladi."
    )
    if not is_pro:
        text += (
            "\n\n━━━━━━━━━━━━━━━\n💎 <b>Kaworai Pro</b> obunasini sotib oling — sifatli va muammosiz tomosha qiling!"
        )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data=f"problems_{anime_id}_{episode}", style="primary")]
        ]
    )
    try:
        await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  RATING
# ═══════════════════════════════════════════════════════════


@callback_router.callback_query(F.data.startswith("rate_"))
async def rate_anime(call: CallbackQuery):
    anime_id = int(call.data.split("_")[1])
    async with AsyncSessionLocal() as session:
        user_rating = await get_user_rating(session, anime_id, call.from_user.id)
    if user_rating:
        return await call.answer("✅ Siz allaqachon baho bergansiz!", show_alert=True)

    rows = []
    row = []
    for i in range(1, 11):
        row.append(InlineKeyboardButton(text=str(i), callback_data=f"score_{anime_id}_{i}", style="primary"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Bekor", callback_data=f"ep_{anime_id}_cancel", style="danger")])

    await call.message.answer(
        "⭐ <b>Baho bering (1-10):</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML"
    )
    await call.answer()


@callback_router.callback_query(F.data.startswith("score_"))
async def save_score(call: CallbackQuery):
    parts = call.data.split("_")
    anime_id = int(parts[1])
    score = int(parts[2])

    async with AsyncSessionLocal() as session:
        existing = await get_user_rating(session, anime_id, call.from_user.id)
        if existing:
            return await call.answer("✅ Allaqachon baho bergansiz!", show_alert=True)
        new_avg = await add_or_update_rating(session, anime_id, call.from_user.id, score)
        anime = await session.get(Anime, anime_id)

    await call.message.edit_text(
        f"✅ <b>Baho qabul qilindi!</b>\n\n"
        f"🎬 <b>{anime.title if anime else anime_id}</b>\n"
        f"⭐ Sizning bahoyingiz: <b>{score}/10</b>\n"
        f"📊 O'rtacha: <b>{new_avg}/10</b>",
        parse_mode="HTML",
    )
    await call.answer(f"⭐ {score}/10 — Rahmat!", show_alert=True)


@callback_router.callback_query(F.data.startswith("rated_"))
async def already_rated(call: CallbackQuery):
    await call.answer("✅ Siz allaqachon baho bergansiz!", show_alert=True)


# ═══════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════


@callback_router.callback_query(F.data == "main_menu")
async def back_to_main(call: CallbackQuery):
    from handlers.users import PHOTO_URL, _get_user_start_extras, get_main_menu_keyboard

    try:
        await call.message.delete()
    except Exception:
        pass
    extras = await _get_user_start_extras(call.from_user.id)
    await call.message.answer_photo(
        photo=PHOTO_URL,
        caption="🎌 <b>Kaworai Anime Bot</b>",
        reply_markup=get_main_menu_keyboard(extras),
        parse_mode="HTML",
    )
    await call.answer()


@callback_router.callback_query(F.data == "no_episodes")
async def no_episodes_cb(call: CallbackQuery):
    await call.answer("⏳ Qismlar hali qo'shilmagan!", show_alert=True)


# ═══════════════════════════════════════════════════════════
#  WATCH (pro_user boshqa joylardan chaqirganda)
# ═══════════════════════════════════════════════════════════


@callback_router.callback_query(F.data.startswith("watch_") & ~F.data.startswith("watch_start_"))
async def watch_anime(call: CallbackQuery):
    raw = call.data.replace("watch_", "")
    parts = raw.split("_")
    anime_id = int(parts[0])
    ep_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    user_id = call.from_user.id
    is_pro = await _is_pro(user_id)

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return await call.answer("❌ Topilmadi!", show_alert=True)
        if getattr(anime, "is_pro_locked", False) and not is_pro:
            return await call.answer("🔒 Faqat Pro uchun!", show_alert=True)

        result = await session.execute(select(Series).where(Series.anime_id == anime_id).order_by(Series.episode.asc()))
        episodes = result.scalars().all()
        user_rating = await get_user_rating(session, anime_id, user_id)
        subscribed = await is_subscribed_anime(session, anime_id, user_id)

    if not episodes:
        return await call.answer("❌ Hali qismlar qo'shilmagan!", show_alert=True)

    ep = next((e for e in episodes if e.episode == ep_num), episodes[0]) if ep_num else episodes[0]
    total = len(episodes)
    max_ep = max(e.episode for e in episodes)
    is_last = ep.episode == max_ep
    user_rated = user_rating is not None

    kb = _player_kb(anime_id, ep.episode, total, max_ep, is_last, user_rated, subscribed, is_pro)
    caption = f"🎬 <b>{anime.title}</b>\n▶️ {ep.episode}-qism  |  📺 Jami: {total} qism"

    async with AsyncSessionLocal() as session:
        await add_to_watch_history(session, user_id, anime_id, ep.episode)

    await _send_or_edit_video(call, ep.file_id, caption, kb, is_pro)
    await call.answer()
