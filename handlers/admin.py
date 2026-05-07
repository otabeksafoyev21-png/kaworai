import asyncio
import logging
import os
import re as _re_ep
from datetime import datetime, timedelta

from aiogram import Bot, F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from data import config
from database.engine import AsyncSessionLocal
from database.models import Admin, Anime, RelatedContent, Series, SubscriptionChannel, User
from database.queries import (
    add_channel,
    add_partner,
    count_owner_animes,
    get_all_channels,
    get_all_partners,
    get_news_channels,
    is_partner,
    remove_channel,
    remove_partner,
    toggle_channel,
)
from handlers.genres import GENRES, normalize_genre
from handlers.users import mark_admin_active, mark_admin_inactive
from states.admin_states import (
    AddAdState,
    AddAnime,
    AddChannel,
    AddEpisodeState,
    AdminProState,
    BackupState,
    BroadcastState,
    EditAnime,
    EditProTextState,
)
from utils.broadcast import send_with_retry
from utils.genre_picker import genre_picker_kb, genre_picker_text
from utils.regions import is_valid_region, region_label, region_picker_kb
from utils.security import esc, parse_admin_ids

# Admin upload chegaralari (200k user scale'da bot diskini to'ldirib yoki
# Telegram API timeout'ga uchramasligi uchun).
#
# MAX_POSTER_BYTES — Telegram'dan kelgan photo meta'sida file_size bor;
# juda katta rasmni (masalan 20 MB) boshqa userlarga yuborish ham bot'ni
# sekinlashtiradi. 10 MB amalda yetarlidan ko'p.
#
# MAX_TRAILER_BYTES — Telegram video upload limiti 2 GB, lekin bot uchun
# 200 MB yetadi va 2 GB trailer 200k userga yuborilishi Telegram API'ni
# soatlab band qiladi.
#
# MAX_TRAILER_DURATION_SEC — trailerlar odatda 2-3 daqiqa, lekin admin
# noto'g'ri fayl yuborishi mumkin; 30 daqiqa cheki yetarlidan ko'p.
MAX_POSTER_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_TRAILER_BYTES = 200 * 1024 * 1024  # 200 MB
MAX_TRAILER_DURATION_SEC = 30 * 60  # 30 daqiqa


def _poster_rejection_reason(photo_size) -> str | None:
    """Photo o'lchami katta bo'lsa, rad etish sababini qaytaradi."""
    size = getattr(photo_size, "file_size", None) or 0
    if size > MAX_POSTER_BYTES:
        mb = size / (1024 * 1024)
        limit_mb = MAX_POSTER_BYTES / (1024 * 1024)
        return f"❌ Rasm juda katta: {mb:.1f} MB (chegara {limit_mb:.0f} MB)."
    return None


def _trailer_rejection_reason(video) -> str | None:
    """Video o'lchami/davomiyligi katta bo'lsa, rad etish sababini qaytaradi."""
    size = getattr(video, "file_size", None) or 0
    if size > MAX_TRAILER_BYTES:
        mb = size / (1024 * 1024)
        limit_mb = MAX_TRAILER_BYTES / (1024 * 1024)
        return f"❌ Video juda katta: {mb:.1f} MB (chegara {limit_mb:.0f} MB)."
    duration = getattr(video, "duration", None) or 0
    if duration > MAX_TRAILER_DURATION_SEC:
        mins = duration / 60
        limit_mins = MAX_TRAILER_DURATION_SEC / 60
        return f"❌ Video juda uzun: {mins:.1f} daqiqa (chegara {limit_mins:.0f} daqiqa)."
    return None


logger = logging.getLogger(__name__)

admin_router = Router()

# `parse_admin_ids` bo'sh stringlarni filtrlaydi — bu muhim, chunki
# `"".split(",")` list `[""]` ni qaytaradi va bu avtorizatsiya mantig'ida
# xatolarga sabab bo'lishi mumkin.
ADMINS = parse_admin_ids(os.getenv("ADMIN_ID", ""))
SECRET_CHANNEL_ID = config.SECRET_CHANNEL_ID
# Tur bo'yicha alohida maxfiy kanallar — auto-add'da kontent turiga qarab
# kerakli kanalga yuborish uchun. Agar tegishli kanal 0/yo'q bo'lsa,
# `SECRET_CHANNEL_ID` ga fallback bo'ladi (`_resolve_secret_channel`).
SECRET_ANIME_CHANNEL_ID = getattr(config, "SECRET_ANIME_CHANNEL_ID", 0) or 0
SECRET_DORAMA_CHANNEL_ID = getattr(config, "SECRET_DORAMA_CHANNEL_ID", 0) or 0
SECRET_SERIAL_CHANNEL_ID = getattr(config, "SECRET_SERIAL_CHANNEL_ID", 0) or 0
SECRET_KINO_CHANNEL_ID = getattr(config, "SECRET_KINO_CHANNEL_ID", 0) or 0
NEWS_CHANNEL_ID = config.NEWS_CHANNEL_ID
PREVIEW_CHANNEL_ID = getattr(config, "PREVIEW_CHANNEL_ID", 0) or 0


# `Anime.content_type` qiymatlari → atlas-mapping. Bot kontent qo'shishda
# `anime`, `dorama`, `serial`, `movie` (kino) qabul qiladi. Boshqa har qanday
# turdagi yozuvlar uchun (yoki content_type=None) — fallback ishlaydi.
def _resolve_secret_channel(content_type: str | None) -> int:
    """
    Anime turiga qarab maxfiy kanal ID qaytaradi.

    Tartib: tur bo'yicha aniq kanal → `SECRET_CHANNEL_ID` (fallback).
    0 qaytarmaydi (chunki bunday holda send_*  xatoga tushadi); agar hech
    biri sozlanmagan bo'lsa 0 qaytaradi (caller ko'rib qoladi).
    """
    ct = (content_type or "").strip().lower()
    by_type = {
        "anime": SECRET_ANIME_CHANNEL_ID,
        "dorama": SECRET_DORAMA_CHANNEL_ID,
        "serial": SECRET_SERIAL_CHANNEL_ID,
        "movie": SECRET_KINO_CHANNEL_ID,
        "kino": SECRET_KINO_CHANNEL_ID,  # eski yozuvlar bo'lishi mumkin
    }
    chan = by_type.get(ct, 0) or 0
    if chan:
        return chan
    return SECRET_CHANNEL_ID or 0


# `add_episode_from_channel` filtri uchun — sozlangan barcha maxfiy
# kanallar to'plami (0'dan boshqa qiymatlar). Bot bu kanallarning
# birortasiga post qo'yilsa, avto-add ishga tushadi.
def _all_secret_channel_ids() -> set[int]:
    ids: set[int] = set()
    for cid in (
        SECRET_CHANNEL_ID,
        SECRET_ANIME_CHANNEL_ID,
        SECRET_DORAMA_CHANNEL_ID,
        SECRET_SERIAL_CHANNEL_ID,
        SECRET_KINO_CHANNEL_ID,
    ):
        if cid:
            ids.add(int(cid))
    return ids


_SECRET_CHANNEL_IDS = _all_secret_channel_ids()
BOT_USERNAME = os.getenv("BOT_USERNAME", "kaworai_uz_bot")

# Admin pastki reply-keyboard tugmalari.
# Admin panelda har qanday FSM state (masalan `AdminProState.waiting_user_id`
# yoki `AddAnime.waiting_title`) ichida turib, adminlar shu tugmalardan
# birini bossa — state avtomatik tozalanadi va tugmaning o'z handler'i
# ishlaydi. Aks holda state handler tugma matnini "javob" deb qabul qiladi
# (masalan `pm_id_received` uni raqam emas deb rad etardi) va admin
# buyruqlari "ishlamayapti" deb ko'rinardi.
ADMIN_REPLY_BUTTONS: set[str] = {
    "➕ Kontent qo'shish",
    "🎞 Qism qo'shish",
    "🗂 Kontent boshqaruv",
    "📢 Kanallar",
    "📊 Statistika",
    "✉️ Xabar yuborish",
    "👑 Pro boshqaruv",
    "🏆 Top 18",
    "🗄 Baza zaxira",
    "📣 Reklamalar",
    "🌍 Regionlar",
    "📝 Pro yozuvi",
    "🔙 Chiqish",
    "🚫 Bekor qilish",
}

# ─── Admin ruxsatlari (per-admin permissions) ─────────────────────────────
#
# Owner (`ADMIN_ID`) har doim hamma ruxsatga ega. Qo'shimcha adminlarga
# owner tanlagan ruxsat bo'limlarigina ochiladi. Eski adminlar uchun
# `permissions` NULL bo'lsa — backward-compatible tarzda hamma ruxsat
# berilgan deb qaraladi (ekspluatatsiyada buzilmasin).

PERMS_ALL: list[tuple[str, str]] = [
    ("add_anime", "➕ Kontent qo'shish"),
    ("add_episode", "🎞 Qism qo'shish"),
    ("anime_manage", "🗂 Kontent boshqaruv"),
    ("channels", "📢 Kanallar"),
    ("stats", "📊 Statistika"),
    ("broadcast", "✉️ Xabar yuborish"),
    ("pro_manage", "👑 Pro boshqaruv"),
    ("top18", "🏆 Top 18"),
    ("backup", "🗄 Baza zaxira"),
]
PERMS_KEYS: set[str] = {k for k, _ in PERMS_ALL}

# Reply tugma → ruxsat kaliti mapping (middleware'da tekshiramiz)
_BUTTON_TO_PERM: dict[str, str] = {
    "➕ Kontent qo'shish": "add_anime",
    "🎞 Qism qo'shish": "add_episode",
    "🗂 Kontent boshqaruv": "anime_manage",
    "📢 Kanallar": "channels",
    "📊 Statistika": "stats",
    "✉️ Xabar yuborish": "broadcast",
    "👑 Pro boshqaruv": "pro_manage",
    "🏆 Top 18": "top18",
    "🗄 Baza zaxira": "backup",
}


async def get_admin_perms(user_id: int) -> list[str] | None:
    """
    Adminni ruxsatlar ro'yxatini qaytaradi. Owner uchun `None` — "hamma"
    degan ma'noda (tekshiruvlarda True). Adminda `permissions=NULL` bo'lsa
    ham `None` qaytaramiz (eski ma'lumotlar uchun backward-compat).
    Admin emas — bo'sh ro'yxat.
    """
    if _is_owner(user_id):
        return None
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(Admin).where(Admin.telegram_id == user_id))).scalar_one_or_none()
    if row is None:
        return []
    perms = getattr(row, "permissions", None)
    if perms is None:
        return None  # backward-compat: hammasi
    # DB'dan list kutamiz; noto'g'ri tur kelsa — bo'sh
    return [p for p in perms if isinstance(p, str)] if isinstance(perms, list) else []


async def has_permission(user_id: int, perm: str) -> bool:
    """Owner har doim True. Admin `permissions` NULL bo'lsa — True. Aks holda — ro'yxatda bo'lsa True."""
    perms = await get_admin_perms(user_id)
    if perms is None:
        return True
    return perm in perms


def _perm_picker_kb(selected: list[str]) -> InlineKeyboardMarkup:
    """Ruxsatlarni tugmalar orqali tanlash: ikki ustunli, ✅/▫️ toggle + Saqlash/Bekor."""
    sel = set(selected or [])
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for key, label in PERMS_ALL:
        mark = "✅" if key in sel else "▫️"
        pair.append(InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"perm_tog_{key}", style="primary"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append(
        [
            InlineKeyboardButton(text="☑️ Hammasi", callback_data="perm_all", style="success"),
            InlineKeyboardButton(text="▫️ Tozalash", callback_data="perm_clear", style="primary"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="💾 Saqlash", callback_data="perm_save", style="success"),
            InlineKeyboardButton(text="❌ Bekor", callback_data="perm_cancel", style="danger"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.message.outer_middleware()
async def _admin_button_state_reset(handler, event, data):
    """
    Admin reply-keyboard tugmalari har qanday FSM state ichida ham ishlasin
    uchun: agar admin tugmalardan birini bossa — state tozalanadi, shundan
    so'ng odatiy handler routing davom etadi va aynan tugmaning handler'i
    mos keladi. Oddiy foydalanuvchilar uchun ham xavfsiz — ular bu matnlarni
    yuborishlari juda kam uchraydi va tugma handler'lari o'zida
    `is_admin` tekshiruvini bajaradi.
    """
    # Har bir admin router'ga kelgan xabarni log'ga yozamiz — Railway'da
    # "tugma bosdim — bot jim" muammosini aniq ajratish uchun: agar bu log
    # chiqmayotgan bo'lsa — xabar boshqa routerda qolib ketayapti yoki
    # polling bo'lmayapti. Chiqayotgan bo'lsa — FSM filter muammosi.
    if isinstance(event, Message):
        try:
            logger.info(
                "admin_router msg uid=%s text=%r",
                event.from_user.id if event.from_user else None,
                (event.text or event.caption or "<non-text>")[:80],
            )
        except Exception:
            pass

    if isinstance(event, Message) and event.text in ADMIN_REPLY_BUTTONS:
        state: FSMContext | None = data.get("state")
        if state is not None:
            current = None
            try:
                current = await state.get_state()
            except Exception:
                logger.exception(
                    "admin middleware: state.get_state() failed user=%s text=%s",
                    event.from_user.id if event.from_user else None,
                    event.text,
                )
            if current is not None:
                try:
                    await state.clear()
                    logger.info(
                        "admin button '%s' cleared state=%s user=%s",
                        event.text,
                        current,
                        event.from_user.id if event.from_user else None,
                    )
                except Exception:
                    # State clear bo'lmasa ham — handler routing'ni bloklamaymiz.
                    # Aks holda FSM storage yiqilganda ("Redis down") admin
                    # hech qanday tugmadan foydalana olmay qoladi.
                    logger.exception(
                        "admin middleware: state.clear() failed user=%s text=%s",
                        event.from_user.id if event.from_user else None,
                        event.text,
                    )

        # Ruxsat tekshiruvi — admin (owner emas) tugmani bossa-yu unga ruxsat
        # berilmagan bo'lsa, handler'gacha o'tkazmaymiz va qisqa ogohlantirish
        # ko'rsatamiz. Bu eng toza usul: har bir handler'ga `has_permission`
        # qo'shmay, bitta joyda gating qilamiz.
        perm_key = _BUTTON_TO_PERM.get(event.text)
        if perm_key and event.from_user:
            try:
                if not await has_permission(event.from_user.id, perm_key):
                    try:
                        await event.answer("⛔ Sizda bu bo'limga ruxsat yo'q. Asosiy admin bilan bog'laning.")
                    except Exception:
                        logger.exception("admin middleware: no-perm reply failed")
                    return  # handler'ga o'tkazmaymiz
            except Exception:
                # Ruxsat DB'sida xato bo'lsa ham tugmani bloklab qo'ymaymiz.
                logger.exception(
                    "admin middleware: has_permission() failed user=%s perm=%s",
                    event.from_user.id,
                    perm_key,
                )
    return await handler(event, data)


def _is_owner(user_id: int) -> bool:
    return str(user_id) in ADMINS


async def is_admin(user_id: int) -> bool:
    """
    Admin yoki hamkor (partner) bo'lsa — True. Ikkalasi ham admin panelning
    kontent/qism/kanallar oqimlaridan foydalanadi. Farq:
    - Admin: barcha kontentni ko'radi, global kanallar qo'shadi.
    - Partner: faqat o'z kontenti va kanallari bilan ishlaydi (owner_id scoping).
    Owner ro'lini ajratish uchun `_get_owner_ctx()` va `is_partner_user()` bor.
    """
    if _is_owner(user_id):
        return True
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(Admin).where(Admin.telegram_id == user_id))
        return r.scalar_one_or_none() is not None


async def is_partner_user(user_id: int) -> bool:
    """admins.role='partner' bo'lgan user (hamkor) — True."""
    if _is_owner(user_id):
        return False
    async with AsyncSessionLocal() as session:
        return await is_partner(session, user_id)


async def _get_owner_ctx(user_id: int) -> int | None:
    """
    Kontent/kanal qo'shganda `owner_id` uchun qiymat:
    - None: bosh admin yoki oddiy admin (global).
    - user_id: hamkor (partner) — faqat o'z kontenti va kanallari.
    """
    if _is_owner(user_id):
        return None
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(Admin).where(Admin.telegram_id == user_id))).scalar_one_or_none()
        if row and getattr(row, "role", None) == "partner":
            return user_id
    return None


def _yn_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha", callback_data=yes_cb, style="success"),
                InlineKeyboardButton(text="❌ Yo'q", callback_data=no_cb, style="danger"),
            ]
        ]
    )


def _skip_kb(skip_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data=skip_cb, style="primary")]]
    )


# ─── Duplicate title / season picker ──────────────────────────────────
#
# Admin yangi kontent qo'shayotganda, bot bazada aynan shu nomdagi
# kontent bor-yo'qligini tekshiradi (fasl qo'shimchasini e'tiborsiz
# qoldirib). Agar bor bo'lsa — bu anime'ning nechinchi fasli ekanligini
# tugmalar orqali so'raydi. "1-fasl" tanlansa — bu takroriy kontent,
# qayta qo'shishga yo'l qo'yilmaydi va admin'dan boshqa kontent so'raladi.

_SEASON_SUFFIX_RE = _re_ep.compile(r"\s+\d+\s*-?\s*fasl\s*$", _re_ep.IGNORECASE)


def _strip_season_suffix(title: str) -> str:
    """'Naruto 2-fasl' → 'Naruto'. Fasl qo'shimchasi bo'lmasa — o'zi."""
    return _SEASON_SUFFIX_RE.sub("", title or "").strip()


def _season_picker_kb() -> InlineKeyboardMarkup:
    """Fasl tanlash: 2..6 + 'Yo'q, bu boshqa anime' + 'Bekor'."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="2-fasl", callback_data="asn_2", style="success"),
            InlineKeyboardButton(text="3-fasl", callback_data="asn_3", style="success"),
            InlineKeyboardButton(text="4-fasl", callback_data="asn_4", style="success"),
        ],
        [
            InlineKeyboardButton(text="5-fasl", callback_data="asn_5", style="success"),
            InlineKeyboardButton(text="6-fasl", callback_data="asn_6", style="success"),
            InlineKeyboardButton(text="➕ Boshqa", callback_data="asn_more", style="primary"),
        ],
        [
            InlineKeyboardButton(text="1️⃣ Bu 1-fasl (takror)", callback_data="asn_1", style="danger"),
        ],
        [
            InlineKeyboardButton(text="🆕 Yo'q, bu boshqa anime", callback_data="asn_other", style="primary"),
            InlineKeyboardButton(text="🚫 Bekor", callback_data="asn_cancel", style="danger"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _psych_level_kb() -> InlineKeyboardMarkup:
    """Psixologik daraja: 0..10 tugmalar (2 qator) + 'O'tkazib yuborish'."""
    row1 = [InlineKeyboardButton(text=str(i), callback_data=f"apsy_{i}", style="primary") for i in range(6)]
    row2 = [InlineKeyboardButton(text=str(i), callback_data=f"apsy_{i}", style="primary") for i in range(6, 11)]
    rows = [
        row1,
        row2,
        [InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data="apsy_skip", style="primary")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _watch_url(anime_id) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=anime_{anime_id}"


def _watch_kb(anime_id, extra_button: dict | None = None) -> InlineKeyboardMarkup:
    """
    Kanalga yuboriladigan anime post uchun button qator. `extra_button`
    kelganda (dict: {"text": str, "url": str}) u "Ko'rish" oldida alohida
    qatorga qo'shiladi. Bu admin anime post yuborishda ixtiyoriy link
    qo'shmoqchi bo'lsa (masalan "Izoh" yoki "Trailer YouTube").
    """
    rows: list[list[InlineKeyboardButton]] = []
    if extra_button and extra_button.get("text") and extra_button.get("url"):
        rows.append([InlineKeyboardButton(text=extra_button["text"], url=extra_button["url"], style="primary")])
    rows.append([InlineKeyboardButton(text="🧧 Ko'rish", url=_watch_url(anime_id), style="success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _yn(val: bool) -> str:
    return "Ha" if val else "Yo'q"


def _build_post_caption(anime: Anime) -> str:
    """
    Anime uchun to'liq post caption quradi. Admin so'ragan bo'yicha
    animening barcha ma'lumotlari (tur, nom, yil, janr, tag, mood, yulduz,
    ovozlar, qism soni, davomiylik, holat, mashhurlik, Pro lock, yashirin
    gem, tavsif) chiqadi — cheklashlar olib tashlandi, faqat tavsif
    Telegram caption limitiga sig'ishi uchun 600 belgigacha qisqartiriladi.
    """
    type_emoji = {"anime": "🎌", "movie": "🎥", "serial": "📺", "dorama": "🌸"}
    type_label = {"anime": "Anime", "movie": "Kino", "serial": "Serial", "dorama": "Dorama"}
    emoji = type_emoji.get(anime.content_type or "anime", "🎬")
    type_str = type_label.get(anime.content_type or "anime", "—")

    genres_str = ", ".join(anime.genres or []) or "—"
    tags_str = ", ".join(anime.tags or [])
    mood_str = ", ".join(anime.mood or [])
    status_map = {"completed": "✅ Tugagan", "ongoing": "📡 Davom etmoqda", "announced": "📢 Kutilmoqda"}
    status_str = status_map.get(anime.status or "", "")

    lines: list[str] = []
    title_line = f"{emoji} <b>{anime.title}</b>"
    if anime.year:
        title_line += f" ({anime.year})"
    lines.append(title_line)
    lines.append(f"🎬 Tur: {type_str}")
    lines.append(f"🆔 <code>{anime.id}</code>")
    lines.append(f"🎭 Janr: {genres_str}")
    if tags_str:
        lines.append(f"🏷 Tag: {tags_str}")
    if mood_str:
        lines.append(f"😌 Kayfiyat: {mood_str}")

    meta_parts: list[str] = []
    if anime.rating is not None:
        rc = anime.rating_count or 0
        meta_parts.append(f"⭐ {float(anime.rating):.1f}" + (f" ({rc})" if rc else ""))
    if anime.episodes_count:
        meta_parts.append(f"🎞 {anime.episodes_count} qism")
    elif anime.total_episodes:
        meta_parts.append(f"🎞 {anime.total_episodes} qism")
    if anime.duration:
        meta_parts.append(f"⏱ {anime.duration} daq")
    if status_str:
        meta_parts.append(status_str)
    if meta_parts:
        lines.append("  ".join(meta_parts))

    extra_parts: list[str] = []
    if anime.views:
        extra_parts.append(f"👁 {anime.views}")
    if anime.popularity:
        try:
            extra_parts.append(f"🔥 {float(anime.popularity):.1f}")
        except Exception:
            pass
    if extra_parts:
        lines.append("  ".join(extra_parts))

    flags: list[str] = []
    if anime.is_pro_locked:
        flags.append("🔒 Pro")
    if anime.is_hidden_gem:
        flags.append("💎 Hidden gem")
    if flags:
        lines.append("  ".join(flags))

    desc = (anime.description or "").strip()
    if desc:
        if len(desc) > 600:
            desc = desc[:600].rstrip() + "…"
        lines.append(f"\n📖 {desc}")
    return "\n".join(lines)


async def _send_anime_post(
    bot: Bot,
    ch,
    anime: Anime,
    msg: Message = None,
    extra_button: dict | None = None,
    custom_caption: str | None = None,
    media_kind: str = "auto",
) -> bool:
    """
    Kanalga anime post yuboradi — POSTER yoki TREYLER (hech qachon ikkalasi ham birga emas).

    `media_kind`:
      - "poster"  → faqat poster (agar mavjud bo'lsa), aks holda matn
      - "trailer" → faqat treyler video (agar mavjud bo'lsa), aks holda matn
      - "text"    → faqat matn
      - "auto"    → poster bor bo'lsa poster, aks holda treyler, aks holda matn

    `extra_button`: ixtiyoriy {"text": ..., "url": ...} — "Ko'rish" oldidan
    qo'shimcha button chiqadi. `custom_caption`: bo'lmasa
    `_build_post_caption` avto tuziladi.
    """
    caption = custom_caption if custom_caption is not None else _build_post_caption(anime)
    watch_kb = _watch_kb(anime.id, extra_button=extra_button)

    kind = (media_kind or "auto").lower()
    if kind == "auto":
        if anime.poster_file_id:
            kind = "poster"
        elif anime.trailer_file_id:
            kind = "trailer"
        else:
            kind = "text"

    try:
        if kind == "poster" and anime.poster_file_id:
            await bot.send_photo(
                chat_id=ch.channel_id,
                photo=anime.poster_file_id,
                caption=caption,
                reply_markup=watch_kb,
                parse_mode="HTML",
            )
        elif kind == "trailer" and anime.trailer_file_id:
            await bot.send_video(
                chat_id=ch.channel_id,
                video=anime.trailer_file_id,
                caption=caption,
                reply_markup=watch_kb,
                parse_mode="HTML",
            )
        else:
            # Tanlangan media mavjud emas yoki "text" — faqat matn yuboramiz.
            await bot.send_message(chat_id=ch.channel_id, text=caption, reply_markup=watch_kb, parse_mode="HTML")
        return True
    except Exception as e:
        if msg:
            await msg.answer(f"⚠️ {ch.channel_name} ga yuborishda xato: {e}")
        return False


# ─── Klaviaturalar ───────────────────────────────────────────────────────────

admin_main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Kontent qo'shish"), KeyboardButton(text="🎞 Qism qo'shish")],
        [KeyboardButton(text="🗂 Kontent boshqaruv"), KeyboardButton(text="📢 Kanallar")],
        [KeyboardButton(text="👑 Pro boshqaruv"), KeyboardButton(text="✉️ Xabar yuborish")],
        [KeyboardButton(text="🗄 Baza zaxira"), KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="🏆 Top 18"), KeyboardButton(text="📣 Reklamalar")],
        [KeyboardButton(text="🌍 Regionlar"), KeyboardButton(text="📝 Pro yozuvi")],
        [KeyboardButton(text="🔙 Chiqish")],
    ],
    resize_keyboard=True,
)

# Hamkor (partner) uchun cheklangan reply klaviatura — faqat o'z
# kontent va kanallari bilan ishlash. Admin panelidagi "Pro boshqaruv",
# "Top 18", "Baza zaxira", "Xabar yuborish" hamkorga ko'rinmaydi.
partner_main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Kontent qo'shish"), KeyboardButton(text="🎞 Qism qo'shish")],
        [KeyboardButton(text="🗂 Kontent boshqaruv"), KeyboardButton(text="📢 Kanallar")],
        [KeyboardButton(text="🔙 Chiqish")],
    ],
    resize_keyboard=True,
)

cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🚫 Bekor qilish")]], resize_keyboard=True)

# Bulk auto-add preview qadamida ishlatiladi: "✅ Tasdiqlash" tugmasi
# pastdagi "🚫 Bekor qilish" yonida (bir qatorda) chiqadi — admin so'roviga
# ko'ra. Inline tugma o'rniga reply keyboard ishlatiladi.
bulk_confirm_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Tasdiqlash"), KeyboardButton(text="🚫 Bekor qilish")],
    ],
    resize_keyboard=True,
)

ADD_STATUS_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📡 Davom etmoqda", callback_data="addstatus_ongoing", style="success")],
        [InlineKeyboardButton(text="✅ Tugagan", callback_data="addstatus_completed", style="success")],
        [InlineKeyboardButton(text="📢 Kutilmoqda", callback_data="addstatus_announced", style="success")],
    ]
)

EDIT_STATUS_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📡 Davom etmoqda", callback_data="editstatus_ongoing", style="success")],
        [InlineKeyboardButton(text="✅ Tugagan", callback_data="editstatus_completed", style="success")],
        [InlineKeyboardButton(text="📢 Kutilmoqda", callback_data="editstatus_announced", style="success")],
    ]
)

TYPE_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🎌 Anime", callback_data="atype_anime", style="success"),
            InlineKeyboardButton(text="🎥 Kino", callback_data="atype_movie", style="success"),
        ],
        [
            InlineKeyboardButton(text="📺 Serial", callback_data="atype_serial", style="success"),
            InlineKeyboardButton(text="🌸 Dorama", callback_data="atype_dorama", style="success"),
        ],
    ]
)


# ═══════════════════════════════════════════════════════════
#  ADMIN KIRISH
# ═══════════════════════════════════════════════════════════


@admin_router.message(Command("admin"))
async def admin_entry(msg: Message, state: FSMContext):
    logger.info("admin_entry hit user_id=%s ADMINS=%s", msg.from_user.id, ADMINS)
    if not await is_admin(msg.from_user.id):
        return await msg.answer("❌ Siz admin emassiz!")

    # /admin buyrug'i har doim toza holatda ochilsin. Agar admin biror
    # tugallanmagan FSM flow'da qolib ketgan bo'lsa (masalan `AddAnime.waiting_title`
    # yoki `AdminRejectState.waiting_reason`), undagi qoldiqni tozalaymiz —
    # aks holda keyingi tugma bosishlari eski state handlerlari tomonidan
    # "eb ketiladi".
    try:
        await state.clear()
    except Exception:
        logger.exception("admin_entry: state.clear() failed user=%s", msg.from_user.id)

    async with AsyncSessionLocal() as session:
        admin = (await session.execute(select(Admin).where(Admin.telegram_id == msg.from_user.id))).scalar_one_or_none()

        if not admin and str(msg.from_user.id) in ADMINS:
            admin = Admin(telegram_id=msg.from_user.id, role="owner", nickname=msg.from_user.full_name)
            session.add(admin)
            await session.commit()

    # Hamkor (partner) — /admin o'rniga partner panelni ko'radi.
    if admin and getattr(admin, "role", None) == "partner":
        await msg.answer(
            "🤝 <b>Hamkor paneli</b>\n\nSiz faqat o'z kontentingiz va kanallaringiz bilan ishlaysiz.",
            reply_markup=partner_main_kb,
            parse_mode="HTML",
        )
        mark_admin_active(msg.from_user.id)
        return

    role_str = admin.role.upper() if admin else "OWNER"

    await msg.answer(
        f"🛠 <b>Kaworai Admin Panel</b>\nRol: {role_str}\n\n"
        "📌 <b>Qo'llanma:</b>\n"
        "• <b>➕ Kontent qo'shish</b> — yangi anime/kino/serial/dorama qo'shish "
        "(ID → nom → tur → tavsif → rasm)\n"
        "• <b>🎞 Qism qo'shish</b> — kontent uchun qismlar yuklash "
        "(birma-bir yoki bulk usulda)\n"
        "• <b>📢 Kanallar</b> — obuna kanallari qo'shish/o'chirish\n"
        "• <b>🗂 Kontent boshqaruv</b> — mavjud kontentni tahrirlash/o'chirish",
        reply_markup=admin_main_kb,
        parse_mode="HTML",
    )

    # 🔥 SHU YERGA QO‘SHASAN
    mark_admin_active(msg.from_user.id)


@admin_router.message(Command("partner"))
async def partner_entry(msg: Message, state: FSMContext):
    """
    Hamkor (partner) paneli. Faqat admins.role='partner' bo'lgan
    foydalanuvchiga ochiladi. Bosh admin ham kirsa bo'ladi — diagnostika uchun.
    """
    try:
        await state.clear()
    except Exception:
        pass
    if not (_is_owner(msg.from_user.id) or await is_partner_user(msg.from_user.id)):
        return await msg.answer("❌ Siz hamkor emassiz.")
    async with AsyncSessionLocal() as session:
        own_count = await count_owner_animes(session, msg.from_user.id)
    await msg.answer(
        "🤝 <b>Hamkor paneli</b>\n\n"
        f"📦 Sizning kontentingiz: <b>{own_count}</b>\n\n"
        "• <b>Kontent qo'shish</b> — yangi anime/kino/serial qo'shish\n"
        "• <b>Qism qo'shish</b> — faqat o'z kontentingizga\n"
        "• <b>Kontent boshqaruv</b> — o'zingiz qo'shganlarni tahrirlash/o'chirish\n"
        "• <b>Kanallar</b> — sizning kontentingizga majburiy obuna kanallari",
        reply_markup=partner_main_kb,
        parse_mode="HTML",
    )
    mark_admin_active(msg.from_user.id)


@admin_router.message(F.text == "🚫 Bekor qilish")
async def cancel_action(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    await state.clear()
    await msg.answer("Amal bekor qilindi.", reply_markup=admin_main_kb)


# ═══════════════════════════════════════════════════════════
#  PRO BOSHQARUV
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "👑 Pro boshqaruv")
async def pro_manage_menu(msg: Message):
    if not await is_admin(msg.from_user.id):
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 User qidirish (ID)", callback_data="pm_user_info", style="success")],
            [InlineKeyboardButton(text="✅ Pro berish", callback_data="pm_set_pro", style="success")],
            [InlineKeyboardButton(text="❌ Pro olish", callback_data="pm_remove_pro", style="danger")],
            [InlineKeyboardButton(text="⭐ Pro userlar ro'yxati", callback_data="pm_pro_list", style="primary")],
            [InlineKeyboardButton(text="━━━━━━━━━━━━━━━━━", callback_data="pm_sep", style="primary")],
            [InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="pm_add_admin", style="success")],
            [InlineKeyboardButton(text="🗑 Admin o'chirish", callback_data="pm_remove_admin", style="danger")],
            [InlineKeyboardButton(text="👥 Adminlar ro'yxati", callback_data="pm_admin_list", style="primary")],
            [InlineKeyboardButton(text="━━━━━━━━━━━━━━━━━", callback_data="pm_sep", style="primary")],
            [InlineKeyboardButton(text="🤝 Hamkor qo'shish", callback_data="pm_add_partner", style="success")],
            [InlineKeyboardButton(text="🗑 Hamkor o'chirish", callback_data="pm_remove_partner", style="danger")],
            [InlineKeyboardButton(text="📋 Hamkorlar ro'yxati", callback_data="pm_partner_list", style="primary")],
            [InlineKeyboardButton(text="━━━━━━━━━━━━━━━━━", callback_data="pm_sep", style="primary")],
            [InlineKeyboardButton(text="📋 Kontent info (ID)", callback_data="pm_anime_info", style="primary")],
            [InlineKeyboardButton(text="📊 Pro statistika", callback_data="pm_stats", style="primary")],
            [InlineKeyboardButton(text="❌ Yopish", callback_data="pm_close", style="danger")],
        ]
    )
    await msg.answer("👑 <b>Pro Boshqaruv</b>", reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data == "pm_sep")
async def pm_sep(call: types.CallbackQuery):
    await call.answer()


@admin_router.callback_query(F.data == "pm_close")
async def pm_close(call: types.CallbackQuery):
    try:
        await call.message.edit_text("✅ Yopildi.")
    except Exception:
        pass
    await call.answer()


@admin_router.callback_query(F.data == "pm_user_info")
async def pm_user_info_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminProState.waiting_user_id)
    await state.update_data(pm_action="user_info")
    await call.message.answer("🔍 User ID kiriting:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.callback_query(F.data == "pm_set_pro")
async def pm_set_pro_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminProState.waiting_user_id)
    await state.update_data(pm_action="set_pro")
    await call.message.answer("✅ <b>Pro berish</b>\n\nUser ID kiriting:", parse_mode="HTML", reply_markup=cancel_kb)
    await call.answer()


@admin_router.callback_query(F.data == "pm_remove_pro")
async def pm_remove_pro_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminProState.waiting_user_id)
    await state.update_data(pm_action="remove_pro")
    await call.message.answer("❌ <b>Pro olish</b>\n\nUser ID kiriting:", parse_mode="HTML", reply_markup=cancel_kb)
    await call.answer()


@admin_router.callback_query(F.data == "pm_anime_info")
async def pm_anime_info_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminProState.waiting_user_id)
    await state.update_data(pm_action="anime_info")
    await call.message.answer("📋 Anime ID kiriting:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.callback_query(F.data == "pm_add_admin")
async def pm_add_admin_start(call: types.CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    await state.set_state(AdminProState.waiting_user_id)
    await state.update_data(pm_action="add_admin")
    await call.message.answer(
        "➕ <b>Admin qo'shish</b>\n\nUser ID kiriting:\n<i>Ixtiyoriy: ID va ism — <code>123456789 Ism</code></i>",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await call.answer()


@admin_router.callback_query(F.data == "pm_remove_admin")
async def pm_remove_admin_start(call: types.CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    await state.set_state(AdminProState.waiting_user_id)
    await state.update_data(pm_action="remove_admin")
    await call.message.answer(
        "🗑 <b>Admin o'chirish</b>\n\nAdmin ID kiriting:", parse_mode="HTML", reply_markup=cancel_kb
    )
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  HAMKOR (PARTNER) BOSHQARUV — faqat owner
# ═══════════════════════════════════════════════════════════


@admin_router.callback_query(F.data == "pm_add_partner")
async def pm_add_partner_start(call: types.CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    await state.set_state(AdminProState.waiting_user_id)
    await state.update_data(pm_action="add_partner")
    await call.message.answer(
        "🤝 <b>Hamkor qo'shish</b>\n\n"
        "Telegram ID kiriting (ixtiyoriy — ism ham):\n"
        "<code>123456789 HamkorIsm</code>\n\n"
        "Hamkor /partner orqali o'z panelini ochadi. U faqat o'zi qo'shgan "
        "kontent va kanallar bilan ishlaydi.",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await call.answer()


@admin_router.callback_query(F.data == "pm_remove_partner")
async def pm_remove_partner_start(call: types.CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    await state.set_state(AdminProState.waiting_user_id)
    await state.update_data(pm_action="remove_partner")
    await call.message.answer(
        "🗑 <b>Hamkor o'chirish</b>\n\nHamkor Telegram ID:",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await call.answer()


@admin_router.callback_query(F.data == "pm_partner_list")
async def pm_partner_list(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    async with AsyncSessionLocal() as session:
        partners = await get_all_partners(session)
        if not partners:
            try:
                await call.message.edit_text("🤝 Hamkorlar yo'q.")
            except Exception:
                pass
            return await call.answer()
        lines = ["🤝 <b>Hamkorlar ro'yxati</b>\n"]
        for p in partners:
            own_count = await count_owner_animes(session, p.telegram_id)
            nick = esc(p.nickname) if p.nickname else "—"
            lines.append(f"• <code>{p.telegram_id}</code> — {nick}  📦 {own_count}")
    try:
        await call.message.edit_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        await call.message.answer("\n".join(lines), parse_mode="HTML")
    await call.answer()


@admin_router.message(AdminProState.waiting_user_id)
async def pm_id_received(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)

    data = await state.get_data()
    action = data.get("pm_action", "")
    parts = msg.text.strip().split()

    if not parts[0].isdigit():
        return await msg.answer("❌ Raqam kiriting!")

    target_id = int(parts[0])

    if action == "user_info":
        await state.clear()
        await _show_user_info(msg, target_id)

    elif action == "set_pro":
        await state.set_state(AdminProState.waiting_pro_days)
        await state.update_data(pro_target_id=target_id)
        await msg.answer(
            f"✅ User <code>{target_id}</code>\n\nNecha kun Pro? Kiriting:\n<i>30 = 30 kun, 0 = Abadiy</i>",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )

    elif action == "remove_pro":
        await state.clear()
        await _do_remove_pro(msg, target_id)

    elif action == "anime_info":
        await state.clear()
        await _show_anime_info(msg, target_id)

    elif action == "add_admin":
        if not _is_owner(msg.from_user.id):
            await state.clear()
            return await msg.answer("❌ Faqat owner!", reply_markup=admin_main_kb)
        if _is_owner(target_id):
            await state.clear()
            return await msg.answer("⚠️ Bu foydalanuvchi allaqachon owner!", reply_markup=admin_main_kb)
        async with AsyncSessionLocal() as session:
            existing = await session.get(Admin, target_id)
        if existing:
            await state.clear()
            return await msg.answer(
                f"⚠️ <code>{target_id}</code> allaqachon admin! Ruxsatlarini o'zgartirish uchun "
                "admin ro'yxatidan tanlang.",
                parse_mode="HTML",
                reply_markup=admin_main_kb,
            )
        nickname = " ".join(parts[1:]) if len(parts) > 1 else None
        # Standart boshlang'ich to'plami — eng xavfsiz minimum (faqat kontent
        # qo'shish). Owner picker'da xohlagan tarzda o'zgartiradi.
        initial = ["add_episode"]
        await state.set_state(AdminProState.waiting_admin_perms)
        await state.update_data(
            perm_target_id=target_id,
            perm_target_nick=nickname,
            perm_selected=initial,
            perm_edit_mode=False,
        )
        await msg.answer(
            f"🔐 <b>Ruxsatlarni tanlang</b> — <code>{target_id}</code>"
            + (f" ({esc(nickname)})" if nickname else "")
            + "\n\nTugmalardan tanlang (✅ — ochiq, ▫️ — yopiq). Oxirida <b>Saqlash</b> bosing.",
            parse_mode="HTML",
            reply_markup=_perm_picker_kb(initial),
        )

    elif action == "remove_admin":
        if not _is_owner(msg.from_user.id):
            await state.clear()
            return await msg.answer("❌ Faqat owner!", reply_markup=admin_main_kb)
        await state.clear()
        await _do_remove_admin(msg, target_id)

    elif action == "add_partner":
        if not _is_owner(msg.from_user.id):
            await state.clear()
            return await msg.answer("❌ Faqat owner!", reply_markup=admin_main_kb)
        if _is_owner(target_id):
            await state.clear()
            return await msg.answer("⚠️ Owner'ni hamkor qilib bo'lmaydi.", reply_markup=admin_main_kb)
        nickname = " ".join(parts[1:]) if len(parts) > 1 else None
        await state.clear()
        async with AsyncSessionLocal() as session:
            _row, created = await add_partner(
                session, telegram_id=target_id, nickname=nickname, added_by=msg.from_user.id
            )
        if created:
            text = (
                f"✅ <b>Hamkor qo'shildi:</b> <code>{target_id}</code>"
                + (f" ({esc(nickname)})" if nickname else "")
                + "\n\nHamkor /partner buyrug'i bilan o'z panelini ochadi."
            )
        else:
            text = f"ℹ️ <code>{target_id}</code> endi hamkor sifatida belgilandi (avval admin edi — roli yangilandi)."
        await msg.answer(text, parse_mode="HTML", reply_markup=admin_main_kb)

    elif action == "remove_partner":
        if not _is_owner(msg.from_user.id):
            await state.clear()
            return await msg.answer("❌ Faqat owner!", reply_markup=admin_main_kb)
        await state.clear()
        async with AsyncSessionLocal() as session:
            ok = await remove_partner(session, target_id)
        if ok:
            await msg.answer(
                f"✅ Hamkor <code>{target_id}</code> o'chirildi.\n\n"
                "Uning kontenti va kanallari bazada qoladi — faqat rol yo'qoladi.",
                parse_mode="HTML",
                reply_markup=admin_main_kb,
            )
        else:
            await msg.answer(f"❌ <code>{target_id}</code> hamkor emas.", parse_mode="HTML", reply_markup=admin_main_kb)


@admin_router.message(AdminProState.waiting_pro_days)
async def pm_pro_days_received(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.strip().isdigit():
        return await msg.answer("❌ Raqam kiriting! (0 = abadiy)")

    data = await state.get_data()
    target_id = data.get("pro_target_id")
    days = int(msg.text.strip())
    await state.clear()
    await _do_set_pro(msg, target_id, days)


async def _do_set_pro(msg: Message, user_id: int, days: int):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await msg.answer(f"❌ User <code>{user_id}</code> topilmadi!", parse_mode="HTML")

        now = datetime.utcnow()
        if days == 0:
            user.pro_until = None
            until_str = "Abadiy"
        else:
            base = user.pro_until if (user.pro_until and user.pro_until > now) else now
            user.pro_until = base + timedelta(days=days)
            until_str = user.pro_until.strftime("%d.%m.%Y")
        user.is_pro = True
        await session.commit()
        name = user.full_name or str(user_id)

    try:
        await msg.bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 <b>Kaworai Pro faollashtirildi!</b>\n\n📅 Tugash: <b>{until_str}</b>\n\n👉 /start → 🟢 Kaworai Pro"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    await msg.answer(
        f"✅ <b>{name}</b> (<code>{user_id}</code>) Pro qilindi!\n📅 {until_str}",
        parse_mode="HTML",
        reply_markup=admin_main_kb,
    )


async def _do_remove_pro(msg: Message, user_id: int):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await msg.answer(f"❌ User <code>{user_id}</code> topilmadi!", parse_mode="HTML")
        user.is_pro = False
        user.pro_until = None
        await session.commit()
        name = user.full_name or str(user_id)

    try:
        await msg.bot.send_message(
            chat_id=user_id, text="❌ <b>Kaworai Pro obunangiz bekor qilindi.</b>", parse_mode="HTML"
        )
    except Exception:
        pass

    await msg.answer(
        f"✅ <b>{name}</b> (<code>{user_id}</code>) Pro olib tashlandi.", parse_mode="HTML", reply_markup=admin_main_kb
    )


async def _show_user_info(msg: Message, user_id: int):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await msg.answer(f"❌ User <code>{user_id}</code> topilmadi!", parse_mode="HTML")

        try:
            from database.models import AnimeSubscription

            sub_count = (
                await session.execute(
                    select(func.count(AnimeSubscription.user_id)).where(AnimeSubscription.user_id == user_id)
                )
            ).scalar() or 0
        except Exception:
            sub_count = 0

    now = datetime.utcnow()
    is_pro = user.is_pro and (not user.pro_until or user.pro_until > now)

    if user.pro_until:
        days_left = (user.pro_until - now).days
        until_full = user.pro_until.strftime("%d.%m.%Y") + f" ({days_left} kun qoldi)"
    else:
        until_full = "Abadiy" if user.is_pro else "—"

    pro_status = "✅ Ha" if is_pro else "❌ Yo'q"
    joined_str = user.joined_at.strftime("%d.%m.%Y") if user.joined_at else "—"
    # Foydalanuvchining full_name va username'i ishonchli bo'lmagan
    # matn — HTML injection'dan himoya uchun ekran qilamiz.
    username = esc(user.username) if user.username else "—"
    full_name = esc(user.full_name) if user.full_name else "—"

    text = (
        f"👤 <b>Foydalanuvchi</b>\n\n"
        f"🆔 ID: <code>{user.telegram_id}</code>\n"
        f"📛 Ism: <b>{full_name}</b>\n"
        f"🔗 @{username}\n"
        f"📅 Ro'yxatdan: {joined_str}\n\n"
        f"⭐ Pro: {pro_status}\n"
        f"📅 Pro tugashi: {until_full}\n"
        f"🔔 Obunalar: {sub_count} ta"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ 30 kun Pro", callback_data=f"adm_pro30_{user_id}", style="success"),
                InlineKeyboardButton(text="✅ 90 kun Pro", callback_data=f"adm_pro90_{user_id}", style="success"),
            ],
            [InlineKeyboardButton(text="❌ Pro olish", callback_data=f"adm_remvpro_{user_id}", style="danger")],
            [
                InlineKeyboardButton(text="📉 7 kun qisq.", callback_data=f"adm_reduce7_{user_id}", style="danger"),
                InlineKeyboardButton(text="📉 30 kun qisq.", callback_data=f"adm_reduce30_{user_id}", style="danger"),
            ],
            [InlineKeyboardButton(text="✉️ Xabar yuborish", callback_data=f"pro_msg_{user_id}", style="primary")],
        ]
    )
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("adm_pro30_"))
async def adm_pro30(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    user_id = int(call.data.replace("adm_pro30_", ""))
    await _do_set_pro_cb(call, user_id, 30)


@admin_router.callback_query(F.data.startswith("adm_pro90_"))
async def adm_pro90(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    user_id = int(call.data.replace("adm_pro90_", ""))
    await _do_set_pro_cb(call, user_id, 90)


@admin_router.callback_query(F.data.startswith("adm_remvpro_"))
async def adm_remvpro(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    user_id = int(call.data.replace("adm_remvpro_", ""))
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await call.answer("❌ Topilmadi!", show_alert=True)
        user.is_pro = False
        user.pro_until = None
        await session.commit()
    try:
        await call.bot.send_message(user_id, "❌ <b>Kaworai Pro bekor qilindi.</b>", parse_mode="HTML")
    except Exception:
        pass
    await call.answer("✅ Pro olib tashlandi!", show_alert=True)


@admin_router.callback_query(F.data.startswith("adm_reduce7_"))
async def adm_reduce7(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    user_id = int(call.data.replace("adm_reduce7_", ""))
    await _do_reduce_pro_cb(call, user_id, 7)


@admin_router.callback_query(F.data.startswith("adm_reduce30_"))
async def adm_reduce30(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    user_id = int(call.data.replace("adm_reduce30_", ""))
    await _do_reduce_pro_cb(call, user_id, 30)


async def _do_set_pro_cb(call: types.CallbackQuery, user_id: int, days: int):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await call.answer("❌ Topilmadi!", show_alert=True)
        now = datetime.utcnow()
        base = user.pro_until if (user.pro_until and user.pro_until > now) else now
        user.pro_until = base + timedelta(days=days)
        user.is_pro = True
        await session.commit()
        until_str = user.pro_until.strftime("%d.%m.%Y")
    try:
        await call.bot.send_message(
            user_id,
            f"🎉 <b>Kaworai Pro faollashtirildi!</b>\n📅 Tugash: <b>{until_str}</b>\n\n👉 /start",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await call.answer(f"✅ {days} kun Pro berildi! ({until_str})", show_alert=True)


async def _do_reduce_pro_cb(call: types.CallbackQuery, user_id: int, days: int):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user or not user.is_pro:
            return await call.answer("❌ User Pro emas!", show_alert=True)
        now = datetime.utcnow()
        if user.pro_until:
            user.pro_until = user.pro_until - timedelta(days=days)
            if user.pro_until <= now:
                user.is_pro = False
                user.pro_until = None
                res = f"Pro {days} kun qisqartirildi — tugadi."
            else:
                res = f"Pro {days} kun qisqartirildi — {user.pro_until.strftime('%d.%m.%Y')}"
        else:
            res = "Abadiy Pro ni qisqartirish mumkin emas!"
        await session.commit()
    await call.answer(res, show_alert=True)


@admin_router.callback_query(F.data == "pm_pro_list")
async def pm_pro_list(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.is_pro == True).order_by(User.pro_until.desc()))
        users = result.scalars().all()

    if not users:
        await call.answer("❌ Pro foydalanuvchilar yo'q!", show_alert=True)
        return

    now = datetime.utcnow()
    text = f"⭐ <b>Pro foydalanuvchilar ({len(users)} ta):</b>\n\n"
    for i, u in enumerate(users[:25], 1):
        uname = f"@{esc(u.username)}" if u.username else "—"
        if u.pro_until:
            days_left = (u.pro_until - now).days
            until_str = u.pro_until.strftime("%d.%m.%Y") + f" ({days_left}k)"
        else:
            until_str = "Abadiy"
        expired = " ⚠️" if (u.pro_until and u.pro_until < now) else ""
        text += f"{i}. <code>{u.telegram_id}</code> {uname} — {until_str}{expired}\n"

    try:
        await call.message.edit_text(text, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, parse_mode="HTML")
    await call.answer()


def _perms_short(perms: list[str] | None) -> str:
    """Ro'yxatdagi adminlar uchun qisqa ko'rinish — '9/9', '3/9' yoki 'hammasi'."""
    if perms is None:
        return "hammasi"
    total = len(PERMS_ALL)
    return f"{len(perms)}/{total}"


@admin_router.callback_query(F.data == "pm_admin_list")
async def pm_admin_list(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Admin))
        admins = result.scalars().all()

    owner_str = ", ".join(f"<code>{o}</code>" for o in ADMINS)
    text = f"👑 <b>Ownerlar:</b> {owner_str}\n\n"

    kb_rows: list[list[InlineKeyboardButton]] = []
    if admins:
        text += f"🛠 <b>Adminlar ({len(admins)} ta):</b>\n\n"
        for i, a in enumerate(admins, 1):
            # Nickname odatda admin tomonidan yoziladi, lekin baribir
            # HTML injection'dan himoya uchun ekran qilamiz.
            nick = esc(a.nickname) if a.nickname else "—"
            perms_val = getattr(a, "permissions", None)
            perms_list = perms_val if isinstance(perms_val, list) else None
            text += f"{i}. <code>{a.telegram_id}</code> — {nick} ({esc(a.role)}, ruxsat: {_perms_short(perms_list)})\n"
            # Owner uchun har bir admin yonida "Ruxsatlarni tahrirlash" tugmasi
            if _is_owner(call.from_user.id):
                kb_rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"✏️ {a.telegram_id} ruxsatlari",
                            callback_data=f"perm_edit_{a.telegram_id}",
                            style="primary",
                        )
                    ]
                )
    else:
        text += "🛠 Qo'shimcha adminlar yo'q."

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await call.answer()


async def _do_add_admin(msg: Message, new_id: int, nickname: str | None, permissions: list[str] | None = None):
    if _is_owner(new_id):
        return await msg.answer("⚠️ Bu foydalanuvchi allaqachon owner!", reply_markup=admin_main_kb)

    async with AsyncSessionLocal() as session:
        existing = await session.get(Admin, new_id)
        if existing:
            return await msg.answer(
                f"⚠️ <code>{new_id}</code> allaqachon admin!", parse_mode="HTML", reply_markup=admin_main_kb
            )
        # `permissions=None` — backward compat (eski mijozlar uchun "hammasi"
        # degan semantika). Yangi flow har doim list yuboradi.
        session.add(Admin(telegram_id=new_id, nickname=nickname, role="admin", permissions=permissions))
        await session.commit()

    # Admin'ga bildirish — qanday bo'limlar ochilgan
    if permissions is None:
        perms_line = "hammasi"
    elif not permissions:
        perms_line = "hech qaysi (owner keyingroq ochadi)"
    else:
        label_map = {k: v for k, v in PERMS_ALL}
        perms_line = ", ".join(label_map.get(p, p) for p in permissions)
    try:
        await msg.bot.send_message(
            new_id,
            "✅ <b>Siz Kaworai botiga admin qilib qo'shildingiz!</b>\n\n"
            f"🔐 <b>Ochiq bo'limlar:</b> {esc(perms_line)}\n\nAdmin panel: /admin",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("_do_add_admin: new admin xabarni qabul qila olmadi")

    nick_str = f" ({nickname})" if nickname else ""
    await msg.answer(
        f"✅ <code>{new_id}</code>{nick_str} admin qilindi!\n🔐 Ruxsatlar: {esc(perms_line)}",
        parse_mode="HTML",
        reply_markup=admin_main_kb,
    )


# ─── Ruxsat picker: toggle / hammasi / tozalash / saqlash / bekor ───────


@admin_router.callback_query(AdminProState.waiting_admin_perms, F.data.startswith("perm_tog_"))
async def perm_toggle(call: types.CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    key = call.data.replace("perm_tog_", "", 1)
    if key not in PERMS_KEYS:
        return await call.answer("Noma'lum ruxsat", show_alert=True)
    data = await state.get_data()
    selected: list[str] = list(data.get("perm_selected") or [])
    if key in selected:
        selected.remove(key)
    else:
        selected.append(key)
    await state.update_data(perm_selected=selected)
    try:
        await call.message.edit_reply_markup(reply_markup=_perm_picker_kb(selected))
    except Exception:
        logger.exception("perm_toggle: edit_reply_markup xato")
    await call.answer()


@admin_router.callback_query(AdminProState.waiting_admin_perms, F.data == "perm_all")
async def perm_all(call: types.CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    selected = [k for k, _ in PERMS_ALL]
    await state.update_data(perm_selected=selected)
    try:
        await call.message.edit_reply_markup(reply_markup=_perm_picker_kb(selected))
    except Exception:
        logger.exception("perm_all: edit_reply_markup xato")
    await call.answer("Hammasi belgilandi")


@admin_router.callback_query(AdminProState.waiting_admin_perms, F.data == "perm_clear")
async def perm_clear(call: types.CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    await state.update_data(perm_selected=[])
    try:
        await call.message.edit_reply_markup(reply_markup=_perm_picker_kb([]))
    except Exception:
        logger.exception("perm_clear: edit_reply_markup xato")
    await call.answer("Tozalandi")


@admin_router.callback_query(AdminProState.waiting_admin_perms, F.data == "perm_cancel")
async def perm_cancel(call: types.CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    await state.clear()
    try:
        await call.message.edit_text("❌ Bekor qilindi.")
    except Exception:
        await call.message.answer("❌ Bekor qilindi.", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(AdminProState.waiting_admin_perms, F.data == "perm_save")
async def perm_save(call: types.CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    data = await state.get_data()
    target_id = data.get("perm_target_id")
    selected: list[str] = list(data.get("perm_selected") or [])
    # Noto'g'ri qiymatlarni filtrlaymiz
    selected = [k for k in selected if k in PERMS_KEYS]
    edit_mode: bool = bool(data.get("perm_edit_mode"))
    nickname = data.get("perm_target_nick")
    await state.clear()

    if target_id is None:
        return await call.answer("Target yo'q", show_alert=True)

    if edit_mode:
        # Mavjud admin'ning ruxsatlarini yangilash
        async with AsyncSessionLocal() as session:
            admin = await session.get(Admin, int(target_id))
            if not admin:
                try:
                    await call.message.edit_text(f"❌ Admin <code>{target_id}</code> topilmadi.", parse_mode="HTML")
                except Exception:
                    await call.message.answer(f"❌ Admin {target_id} topilmadi.")
                return await call.answer()
            admin.permissions = selected
            await session.commit()
        label_map = {k: v for k, v in PERMS_ALL}
        perms_line = ", ".join(label_map.get(p, p) for p in selected) if selected else "hech qaysi"
        try:
            await call.message.edit_text(
                f"✅ <code>{target_id}</code> ruxsatlari yangilandi.\n🔐 {esc(perms_line)}",
                parse_mode="HTML",
            )
        except Exception:
            await call.message.answer(
                f"✅ {target_id} ruxsatlari yangilandi.\n🔐 {perms_line}",
                reply_markup=admin_main_kb,
            )
        # Admin'ga ogohlantirish
        try:
            await call.bot.send_message(
                int(target_id),
                f"🔐 <b>Ruxsatlaringiz yangilandi.</b>\nOchiq bo'limlar: {esc(perms_line)}",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("perm_save: admin'ga bildirib bo'lmadi")
        await call.answer("Saqlandi")
        return

    # Yangi admin qo'shish
    # Message'ni callback.message orqali yaratish — _do_add_admin Message kutadi
    await _do_add_admin(call.message, int(target_id), nickname, permissions=selected)
    await call.answer("Qo'shildi")


@admin_router.callback_query(F.data.startswith("perm_edit_"))
async def perm_edit_start(call: types.CallbackQuery, state: FSMContext):
    """Mavjud adminni ruxsatlarini tahrirlash — pm_admin_list dan kelinadi."""
    if not _is_owner(call.from_user.id):
        return await call.answer("❌ Faqat owner!", show_alert=True)
    try:
        target_id = int(call.data.replace("perm_edit_", "", 1))
    except ValueError:
        return await call.answer("Noto'g'ri ID", show_alert=True)
    async with AsyncSessionLocal() as session:
        admin = await session.get(Admin, target_id)
    if not admin:
        return await call.answer("Admin topilmadi", show_alert=True)
    perms_val = getattr(admin, "permissions", None)
    initial = perms_val if isinstance(perms_val, list) else [k for k, _ in PERMS_ALL]
    nickname = admin.nickname
    await state.set_state(AdminProState.waiting_admin_perms)
    await state.update_data(
        perm_target_id=target_id,
        perm_target_nick=nickname,
        perm_selected=list(initial),
        perm_edit_mode=True,
    )
    try:
        await call.message.answer(
            f"🔐 <b>Ruxsatlarni tahrirlash</b> — <code>{target_id}</code>"
            + (f" ({esc(nickname)})" if nickname else "")
            + "\n\nTugmalardan tanlang. Oxirida <b>Saqlash</b> bosing.",
            parse_mode="HTML",
            reply_markup=_perm_picker_kb(list(initial)),
        )
    except Exception:
        logger.exception("perm_edit_start: xabar yuborish xato")
    await call.answer()


async def _do_remove_admin(msg: Message, target_id: int):
    if _is_owner(target_id):
        return await msg.answer("❌ Owner adminni o'chirib bo'lmaydi!", reply_markup=admin_main_kb)

    async with AsyncSessionLocal() as session:
        admin = await session.get(Admin, target_id)
        if not admin:
            return await msg.answer(
                f"❌ <code>{target_id}</code> admin emas!", parse_mode="HTML", reply_markup=admin_main_kb
            )
        await session.delete(admin)
        await session.commit()

    try:
        await msg.bot.send_message(target_id, "❌ <b>Admin huquqingiz olib tashlandi.</b>", parse_mode="HTML")
    except Exception:
        pass

    await msg.answer(f"✅ <code>{target_id}</code> admin emas endi.", parse_mode="HTML", reply_markup=admin_main_kb)


async def _show_anime_info(msg: Message, anime_id: int):
    from database.queries import get_anime_full_info

    async with AsyncSessionLocal() as session:
        info = await get_anime_full_info(session, anime_id)

    if not info:
        return await msg.answer(f"❌ ID {anime_id} topilmadi!")

    genres_str = ", ".join(info["genres"][:4]) or "—"
    tags_str = ", ".join(info["tags"][:4]) or "—"

    type_emoji = {"anime": "🎌", "movie": "🎥", "serial": "📺", "dorama": "🌸"}
    emoji = type_emoji.get(info["type"], "🎬")

    added_at = info.get("added_at")
    added_at_str = added_at.strftime("%d.%m.%Y %H:%M") if added_at else "Nomalum"
    year_str = f" ({info['year']})" if info.get("year") else ""

    text = (
        f"📋 <b>Anime ma'lumotlari</b>\n\n"
        f"{emoji} <b>{info['title']}</b>{year_str}\n"
        f"🆔 ID: <code>{info['id']}</code>\n"
        f"📁 Tur: {info['type']} | 📊 Status: {info.get('status', '—')}\n\n"
        f"🎭 Janr: {genres_str}\n"
        f"🏷 Teglar: {tags_str}\n\n"
        f"⭐ Reyting: <b>{info['rating']:.1f}</b> ({info['rating_count']} ovoz)\n"
        f"👁 Ko'rishlar: <b>{info['views']}</b>\n"
        f"🎞 Qismlar: <b>{info['episodes_count']}</b>\n\n"
        f"🔔 Obunalar: <b>{info['subscribers']}</b> ta\n"
        f"⭐ Pro obunalar: <b>{info['pro_subscribers']}</b> ta\n\n"
        f"🔒 Pro-locked: {_yn(info['is_pro_locked'])}\n"
        f"💎 Hidden Gem: {_yn(info['is_hidden_gem'])}\n\n"
        f"📅 Qo'shilgan: {added_at_str}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Pro-lock toggle", callback_data=f"adm_prolock_{anime_id}", style="primary"
                ),
                InlineKeyboardButton(text="💎 HGem toggle", callback_data=f"adm_hgem_{anime_id}", style="primary"),
            ],
            [InlineKeyboardButton(text="📢 Kanalga post", callback_data=f"postpick_{anime_id}", style="success")],
        ]
    )

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)

    try:
        if anime and anime.poster_file_id:
            await msg.answer_photo(photo=anime.poster_file_id, caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("adm_prolock_"))
async def adm_prolock_toggle(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    anime_id = int(call.data.replace("adm_prolock_", ""))
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if anime:
            anime.is_pro_locked = not anime.is_pro_locked
            await session.commit()
            s = "🔒 Pro-locked" if anime.is_pro_locked else "🔓 Ochiq"
            await call.answer(f"✅ {anime.title}: {s}", show_alert=True)


@admin_router.callback_query(F.data.startswith("adm_hgem_"))
async def adm_hgem_toggle(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    anime_id = int(call.data.replace("adm_hgem_", ""))
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if anime:
            anime.is_hidden_gem = not anime.is_hidden_gem
            await session.commit()
            s = "💎 Hidden Gem: Ha" if anime.is_hidden_gem else "💎 Hidden Gem: Yo'q"
            await call.answer(f"✅ {anime.title}: {s}", show_alert=True)


@admin_router.callback_query(F.data == "pm_stats")
async def pm_stats(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        total_users = await session.scalar(select(func.count(User.telegram_id)))
        pro_users = await session.scalar(select(func.count(User.telegram_id)).where(User.is_pro == True))
        total_animes = await session.scalar(select(func.count(Anime.id)))
        locked_count = await session.scalar(select(func.count(Anime.id)).where(Anime.is_pro_locked == True))
        ep_count = await session.scalar(select(func.count(Series.id)))
        now = datetime.utcnow()
        expired = await session.scalar(
            select(func.count(User.telegram_id)).where(
                User.is_pro == True, User.pro_until != None, User.pro_until < now
            )
        )
        top3 = (
            await session.execute(select(Anime.title, Anime.views).order_by(Anime.views.desc()).limit(3))
        ).fetchall()

    top3_text = "\n".join(f"  {i + 1}. {r[0]} — {r[1]} ko'rish" for i, r in enumerate(top3))
    text = (
        f"📊 <b>Pro Statistika</b>\n\n"
        f"👤 Jami: <b>{total_users}</b>\n"
        f"⭐ Pro: <b>{pro_users}</b>\n"
        f"  ⚠️ Muddati o'tgan: {expired}\n\n"
        f"🎬 Kontent: <b>{total_animes}</b>\n"
        f"  🔒 Pro-locked: {locked_count}\n"
        f"🎞 Qismlar: <b>{ep_count}</b>\n\n"
        f"🔥 Top 3:\n{top3_text}"
    )
    try:
        await call.message.edit_text(text, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, parse_mode="HTML")
    await call.answer()


@admin_router.callback_query(F.data.startswith("pro_msg_"))
async def pro_msg_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    user_id = int(call.data.replace("pro_msg_", ""))
    await state.set_state(AdminProState.waiting_msg_text)
    await state.update_data(msg_target=user_id)
    await call.message.answer(
        f"✉️ User <code>{user_id}</code> ga xabar yozing:", parse_mode="HTML", reply_markup=cancel_kb
    )
    await call.answer()


@admin_router.message(AdminProState.waiting_msg_text)
async def pro_msg_send(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)

    data = await state.get_data()
    user_id = data.get("msg_target")
    await state.clear()

    prefix = "✉️ <b>Admin xabari:</b>\n\n"
    try:
        if msg.photo:
            await msg.bot.send_photo(
                user_id, msg.photo[-1].file_id, caption=prefix + (msg.caption or ""), parse_mode="HTML"
            )
        elif msg.video:
            await msg.bot.send_video(
                user_id, msg.video.file_id, caption=prefix + (msg.caption or ""), parse_mode="HTML"
            )
        else:
            await msg.bot.send_message(user_id, prefix + (msg.text or ""), parse_mode="HTML")
        await msg.answer(f"✅ Xabar yuborildi ({user_id})", reply_markup=admin_main_kb)
    except Exception as e:
        await msg.answer(f"❌ Yuborib bo'lmadi: {e}", reply_markup=admin_main_kb)


# ═══════════════════════════════════════════════════════════
#  ANIME QO'SHISH
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "➕ Kontent qo'shish")
async def add_anime_start(msg: Message, state: FSMContext):
    logger.info("add_anime_start hit user_id=%s", msg.from_user.id)
    if not await is_admin(msg.from_user.id):
        logger.warning("add_anime_start: user %s is not admin (ADMINS=%s)", msg.from_user.id, ADMINS)
        return
    await state.set_state(AddAnime.waiting_id)
    await msg.answer(
        "➕ <b>Yangi kontent qo'shish</b>\n\n"
        "Qadamlar: ID → Nom → Tur (anime/kino/serial/dorama) → "
        "Tavsif → Rasm → Holat\n\n"
        "🆔 <b>Kontent ID</b>sini kiriting (faqat raqam):\n"
        "<i>Masalan: <code>4345</code></i>",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )


@admin_router.message(AddAnime.waiting_id)
async def process_id(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Faqat raqam!")
    anime_id = int(msg.text)
    async with AsyncSessionLocal() as session:
        if await session.get(Anime, anime_id):
            r = await session.execute(select(func.max(Anime.id)))
            sug = (r.scalar() or 0) + 1
            return await msg.answer(f"❌ ID {anime_id} mavjud!\n💡 Bo'sh ID: <code>{sug}</code>", parse_mode="HTML")
    await state.update_data(anime_id=anime_id)
    await state.set_state(AddAnime.waiting_title)
    await msg.answer("📝 <b>Nomi:</b>\n<i>Masalan: Attack on Titan</i>", parse_mode="HTML")


@admin_router.message(AddAnime.waiting_title)
async def process_title(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    raw_title = (msg.text or "").strip()
    if not raw_title:
        return await msg.answer("❌ Nom bo'sh bo'la olmaydi.")
    base_title = _strip_season_suffix(raw_title)
    # Bazadagi bir xil asosiy nomdagi kontent(lar)ni topish — case-insensitive.
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(Anime.id, Anime.title, Anime.season))).all()
    existing_seasons: list[int] = []
    existing_ids: list[int] = []
    for r in rows:
        if _strip_season_suffix(r.title or "").lower() == base_title.lower():
            existing_seasons.append(int(r.season or 1))
            existing_ids.append(int(r.id))
    await state.update_data(title=raw_title, base_title=base_title, season=1)

    if existing_seasons:
        seasons_text = ", ".join(f"{s}-fasl" for s in sorted(set(existing_seasons)))
        await state.update_data(existing_seasons=sorted(set(existing_seasons)))
        await state.set_state(AddAnime.waiting_season)
        await msg.answer(
            f"⚠️ <b>Bir xil nomdagi kontent topildi:</b> <i>{esc(base_title)}</i>\n"
            f"🎞 Mavjud fasllar: <b>{seasons_text}</b>\n\n"
            "Bu yangi fasl bo'lsa — fasl raqamini tanlang.\n"
            "Agar bu boshqa anime bo'lsa — «🆕 Yo'q, bu boshqa anime».\n"
            "Agar bu aynan o'sha 1-fasl bo'lsa — «1️⃣ Bu 1-fasl» (qayta qo'shib bo'lmaydi).",
            parse_mode="HTML",
            reply_markup=_season_picker_kb(),
        )
        return

    await state.set_state(AddAnime.waiting_type)
    await msg.answer("📁 <b>Tur:</b>", parse_mode="HTML", reply_markup=TYPE_KB)


# ─── Season picker callbacks ──────────────────────────────────────
#
# `waiting_season` holatida admin bosishi mumkin:
#   asn_2..asn_6 — fasl raqami
#   asn_more     — matn orqali fasl raqamini kiritish (7, 8, ...)
#   asn_1        — "bu 1-fasl" (takror) — qo'shishga ruxsat yo'q
#   asn_other    — "yo'q, bu boshqa anime" — nomni o'zgartirib keyingi bosqichga
#   asn_cancel   — jarayonni bekor qilish


@admin_router.callback_query(F.data.startswith("asn_"), AddAnime.waiting_season)
async def season_pick(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return await call.answer()
    action = call.data[4:]  # '2','3',...,'more','1','other','cancel'

    if action == "cancel":
        await state.clear()
        try:
            await call.message.edit_text("🚫 Bekor qilindi.")
        except Exception:
            logger.debug("season_pick cancel: edit_text failed", exc_info=True)
        await call.message.answer("Panel:", reply_markup=admin_main_kb)
        return await call.answer()

    data = await state.get_data()
    base_title = data.get("base_title") or data.get("title") or ""
    existing = set(data.get("existing_seasons") or [])

    if action == "1":
        await state.clear()
        try:
            await call.message.edit_text(
                f"❌ «{esc(base_title)}» — 1-fasl allaqachon bazada bor.\n🆕 Iltimos, boshqa kontent qo'shing.",
                parse_mode="HTML",
            )
        except Exception:
            logger.debug("season_pick 1: edit_text failed", exc_info=True)
        await call.message.answer("Panel:", reply_markup=admin_main_kb)
        return await call.answer("Takror kontent", show_alert=True)

    if action == "other":
        # Bu butunlay boshqa anime — yangi nom so'raymiz.
        await state.update_data(title=None, base_title=None, season=1, existing_seasons=[])
        await state.set_state(AddAnime.waiting_title)
        try:
            await call.message.edit_text(
                "🆕 Tushunarli — bu boshqa kontent. Nomini farqli ko'rsating.\n"
                "<i>Masalan: sarlavhaga izoh qo'shing.</i>",
                parse_mode="HTML",
            )
        except Exception:
            logger.debug("season_pick other: edit_text failed", exc_info=True)
        return await call.answer()

    if action == "more":
        # Matn orqali fasl raqamini kiritishni kutamiz (waiting_season ichidagi message handler).
        try:
            await call.message.edit_text(
                f"🔢 Fasl raqamini yozib yuboring (2..999):\n"
                f"Mavjud fasllar: <b>{', '.join(f'{s}-fasl' for s in sorted(existing)) or '—'}</b>",
                parse_mode="HTML",
            )
        except Exception:
            logger.debug("season_pick more: edit_text failed", exc_info=True)
        return await call.answer()

    # Raqam tugma: '2','3','4','5','6'
    if action.isdigit():
        season = int(action)
        if season in existing:
            return await call.answer(f"❌ {season}-fasl allaqachon bor!", show_alert=True)
        await _apply_season_and_continue(call.message, state, base_title, season)
        return await call.answer()

    return await call.answer()


@admin_router.message(AddAnime.waiting_season)
async def season_manual_input(msg: Message, state: FSMContext):
    """Admin "➕ Boshqa" bosgandan keyin raqamni matn orqali yuborsa."""
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        return await msg.answer("❌ Faqat raqam. Tugmalardan foydalaning yoki 2..999 oralig'ida raqam yozing.")
    season = int(raw)
    if season < 2 or season > 999:
        return await msg.answer("❌ Fasl 2..999 oralig'ida bo'lishi kerak.")
    data = await state.get_data()
    base_title = data.get("base_title") or ""
    existing = set(data.get("existing_seasons") or [])
    if season in existing:
        return await msg.answer(f"❌ {season}-fasl allaqachon bor — boshqa raqam tanlang.")
    await _apply_season_and_continue(msg, state, base_title, season)


async def _apply_season_and_continue(msg: Message, state: FSMContext, base_title: str, season: int) -> None:
    """Fasl tanlangan — title'ni yangilab, type so'rashga o'tamiz."""
    new_title = f"{base_title} {season}-fasl" if season > 1 and base_title else base_title
    await state.update_data(title=new_title, season=season)
    await state.set_state(AddAnime.waiting_type)
    try:
        await msg.edit_text(
            f"✅ Saqlandi: <b>{esc(new_title)}</b>",
            parse_mode="HTML",
        )
    except Exception:
        # Message bo'lsa (matn input) — edit_text ishlamaydi, yangi xabar yuboramiz.
        pass
    await msg.answer("📁 <b>Tur:</b>", parse_mode="HTML", reply_markup=TYPE_KB)


@admin_router.callback_query(F.data.startswith("atype_"), AddAnime.waiting_type)
async def process_type(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(content_type=call.data.replace("atype_", ""))
    await state.set_state(AddAnime.waiting_desc)
    await call.message.answer(
        "📖 <b>Tavsif:</b>\n<i>Masalan: Humanity fights against giants…</i>", parse_mode="HTML", reply_markup=cancel_kb
    )
    await call.answer()


@admin_router.message(AddAnime.waiting_desc)
async def process_desc(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    await state.update_data(desc=msg.text.strip(), selected_genres=[])
    await state.set_state(AddAnime.waiting_genres)
    await msg.answer(
        genre_picker_text([]),
        parse_mode="HTML",
        reply_markup=genre_picker_kb([], prefix="ag"),
    )


@admin_router.callback_query(F.data.startswith("ag_tog:"), AddAnime.waiting_genres)
async def add_genre_toggle(call: types.CallbackQuery, state: FSMContext):
    """Add anime — janrni tanlash/olib tashlash."""
    if not await is_admin(call.from_user.id):
        return await call.answer()
    key = call.data.split(":", 1)[1]
    if key not in GENRES:
        return await call.answer("❌ Noto'g'ri janr", show_alert=False)
    data = await state.get_data()
    selected: list[str] = list(data.get("selected_genres") or [])
    if key in selected:
        selected.remove(key)
    else:
        selected.append(key)
    await state.update_data(selected_genres=selected)
    try:
        await call.message.edit_text(
            genre_picker_text(selected),
            parse_mode="HTML",
            reply_markup=genre_picker_kb(selected, prefix="ag"),
        )
    except Exception:
        # Xabar o'zgartirilmasa — sodir bo'lishi mumkin, lekin baribir xatolik chiqib yiqilmasin.
        logger.debug("add_genre_toggle: edit_text failed", exc_info=True)
    await call.answer()


@admin_router.callback_query(F.data == "ag_cancel", AddAnime.waiting_genres)
async def add_genre_cancel(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return await call.answer()
    await state.clear()
    try:
        await call.message.edit_text("❌ Bekor qilindi.")
    except Exception:
        logger.debug("add_genre_cancel: edit_text failed", exc_info=True)
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data == "ag_done", AddAnime.waiting_genres)
async def add_genre_done(call: types.CallbackQuery, state: FSMContext):
    """Add anime — tanlangan janrlarni tasdiqlab, keyingi bosqichga o'tish.

    Agar `Psixologik` janri tanlangan bo'lsa — darajani (0..10) so'rash
    bosqichiga o'tamiz. Aks holda — darrov teg bosqichiga.
    """
    if not await is_admin(call.from_user.id):
        return await call.answer()
    data = await state.get_data()
    selected: list[str] = list(data.get("selected_genres") or [])
    if not selected:
        return await call.answer("❌ Kamida bitta janr tanlang!", show_alert=True)
    await state.update_data(genres=selected)
    labels = ", ".join(GENRES.get(k, k) for k in selected)
    try:
        await call.message.edit_text(
            f"🎭 <b>Janrlar saqlandi:</b> {esc(labels)}",
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("add_genre_done: edit_text failed", exc_info=True)

    if "Psixologik" in selected:
        await state.set_state(AddAnime.waiting_psych_level)
        await call.message.answer(
            "🧠 <b>Psixologik daraja</b> (0..10):\n"
            "<i>0 — yengil; 5 — o'rtacha; 10 — juda og'ir.\n"
            "Bu Pro AI tavsiyalari uchun ishlatiladi.</i>",
            parse_mode="HTML",
            reply_markup=_psych_level_kb(),
        )
        return await call.answer()

    await state.set_state(AddAnime.waiting_tags)
    await call.message.answer(
        "🏷 <b>Teglar</b>:\n<i>dark, survival, revenge</i>",
        parse_mode="HTML",
        reply_markup=_skip_kb("skip_tags"),
    )
    await call.answer()


# ─── Psychological level callbacks ────────────────────────────────


@admin_router.callback_query(F.data.startswith("apsy_"), AddAnime.waiting_psych_level)
async def psych_level_pick(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return await call.answer()
    action = call.data[5:]
    if action == "skip":
        await state.update_data(psychological_level=None)
        level_txt = "o'tkazib yuborildi"
    else:
        try:
            lvl = int(action)
        except ValueError:
            return await call.answer()
        if lvl < 0 or lvl > 10:
            return await call.answer("❌ 0..10 oralig'ida bo'lishi kerak", show_alert=True)
        await state.update_data(psychological_level=lvl)
        level_txt = f"<b>{lvl}/10</b>"
    try:
        await call.message.edit_text(
            f"🧠 Psixologik daraja: {level_txt}",
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("psych_level_pick: edit_text failed", exc_info=True)
    await state.set_state(AddAnime.waiting_tags)
    await call.message.answer(
        "🏷 <b>Teglar</b>:\n<i>dark, survival, revenge</i>",
        parse_mode="HTML",
        reply_markup=_skip_kb("skip_tags"),
    )
    await call.answer()


@admin_router.message(AddAnime.waiting_psych_level)
async def psych_level_manual_input(msg: Message, state: FSMContext):
    """Admin darajani matn orqali yuborsa (0..10)."""
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        return await msg.answer("❌ Faqat 0..10 oralig'ida raqam. Tugmalardan foydalaning.")
    lvl = int(raw)
    if lvl < 0 or lvl > 10:
        return await msg.answer("❌ Daraja 0..10 oralig'ida bo'lishi kerak.")
    await state.update_data(psychological_level=lvl)
    await state.set_state(AddAnime.waiting_tags)
    await msg.answer(
        f"🧠 Psixologik daraja: <b>{lvl}/10</b>\n\n🏷 <b>Teglar</b>:\n<i>dark, survival, revenge</i>",
        parse_mode="HTML",
        reply_markup=_skip_kb("skip_tags"),
    )


@admin_router.message(AddAnime.waiting_genres)
async def genres_text_hint(msg: Message, state: FSMContext):
    """Admin yozma matn yuborsa — tugmalardan foydalanishga undaydi."""
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    data = await state.get_data()
    selected: list[str] = list(data.get("selected_genres") or [])
    await msg.answer(
        "ℹ️ Iltimos, yuqoridagi tugmalar orqali janrlarni tanlang — matn bilan emas.\n" + genre_picker_text(selected),
        parse_mode="HTML",
        reply_markup=genre_picker_kb(selected, prefix="ag"),
    )


@admin_router.callback_query(F.data == "skip_tags", AddAnime.waiting_tags)
async def skip_tags(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(tags=[])
    await state.set_state(AddAnime.waiting_mood)
    await call.message.answer(
        "😌 <b>Mood</b>:\n<i>dark, emotional</i>", parse_mode="HTML", reply_markup=_skip_kb("skip_mood")
    )
    await call.answer()


@admin_router.message(AddAnime.waiting_tags)
async def process_tags(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    await state.update_data(tags=[t.strip() for t in msg.text.split(",")])
    await state.set_state(AddAnime.waiting_mood)
    await msg.answer("😌 <b>Mood</b>:\n<i>dark, emotional</i>", parse_mode="HTML", reply_markup=_skip_kb("skip_mood"))


@admin_router.callback_query(F.data == "skip_mood", AddAnime.waiting_mood)
async def skip_mood(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(mood=[])
    await state.set_state(AddAnime.waiting_year)
    await call.message.answer(
        "📅 <b>Chiqqan yili:</b>\n<i>Masalan: 2013</i>", parse_mode="HTML", reply_markup=cancel_kb
    )
    await call.answer()


@admin_router.message(AddAnime.waiting_mood)
async def process_mood(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    await state.update_data(mood=[m.strip() for m in msg.text.split(",")])
    await state.set_state(AddAnime.waiting_year)
    await msg.answer("📅 <b>Chiqqan yili:</b>\n<i>Masalan: 2013</i>", parse_mode="HTML")


@admin_router.message(AddAnime.waiting_year)
async def process_year(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Raqam!")
    await state.update_data(year=int(msg.text))
    await state.set_state(AddAnime.waiting_rating)
    await msg.answer("⭐ <b>Reyting</b> (0-10):\n<i>Masalan: 9.0</i>", parse_mode="HTML")


@admin_router.message(AddAnime.waiting_rating)
async def process_rating(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    try:
        r = float(msg.text.replace(",", "."))
        if not (0 <= r <= 10):
            return await msg.answer("❌ 0-10!")
    except ValueError:
        return await msg.answer("❌ Raqam!")
    await state.update_data(rating=r)
    await state.set_state(AddAnime.waiting_total_episodes)
    await msg.answer(
        "🎞 <b>Qismlar soni:</b>\n<i>Masalan: 25</i>", parse_mode="HTML", reply_markup=_skip_kb("skip_episodes")
    )


@admin_router.callback_query(F.data == "skip_episodes", AddAnime.waiting_total_episodes)
async def skip_episodes(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(total_episodes=0, episodes_count=0)
    await state.set_state(AddAnime.waiting_duration)
    await call.message.answer(
        "⏱ <b>Davomiyligi</b> (daqiqada):\n<i>Masalan: 24</i>",
        parse_mode="HTML",
        reply_markup=_skip_kb("skip_duration"),
    )
    await call.answer()


@admin_router.message(AddAnime.waiting_total_episodes)
async def process_total_episodes(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Raqam!")
    ep = int(msg.text)
    await state.update_data(total_episodes=ep, episodes_count=ep)
    await state.set_state(AddAnime.waiting_duration)
    await msg.answer(
        "⏱ <b>Davomiyligi</b> (daqiqada):\n<i>Masalan: 24</i>",
        parse_mode="HTML",
        reply_markup=_skip_kb("skip_duration"),
    )


@admin_router.callback_query(F.data == "skip_duration", AddAnime.waiting_duration)
async def skip_duration(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(duration=None)
    await state.set_state(AddAnime.waiting_status)
    await call.message.answer("📊 <b>Status:</b>", parse_mode="HTML", reply_markup=ADD_STATUS_KB)
    await call.answer()


@admin_router.message(AddAnime.waiting_duration)
async def process_duration(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Raqam!")
    await state.update_data(duration=int(msg.text))
    await state.set_state(AddAnime.waiting_status)
    await msg.answer("📊 <b>Status:</b>", parse_mode="HTML", reply_markup=ADD_STATUS_KB)


@admin_router.callback_query(F.data.startswith("addstatus_"), AddAnime.waiting_status)
async def process_add_status(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(status=call.data.replace("addstatus_", ""))
    await state.set_state(AddAnime.waiting_popularity)
    await call.message.answer(
        "📈 <b>Mashhurlik</b> (0-10):\n<i>Masalan: 8.7</i>", parse_mode="HTML", reply_markup=_skip_kb("skip_popularity")
    )
    await call.answer()


@admin_router.callback_query(F.data == "skip_popularity", AddAnime.waiting_popularity)
async def skip_popularity(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(popularity=0.0)
    await state.set_state(AddAnime.waiting_related)
    await call.message.answer(
        "🔗 <b>Related animelar</b>:\n<i>5001:sequel, 5002:spin-off</i>\n\n"
        "Format: <code>ID:tur</code> — vergul bilan ajrating",
        parse_mode="HTML",
        reply_markup=_skip_kb("skip_related"),
    )
    await call.answer()


@admin_router.message(AddAnime.waiting_popularity)
async def process_popularity(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    try:
        pop = float(msg.text.replace(",", "."))
    except ValueError:
        return await msg.answer("❌ Raqam!")
    await state.update_data(popularity=pop)
    await state.set_state(AddAnime.waiting_related)
    await msg.answer(
        "🔗 <b>Related animelar</b>:\n<i>5001:sequel, 5002:spin-off</i>",
        parse_mode="HTML",
        reply_markup=_skip_kb("skip_related"),
    )


@admin_router.callback_query(F.data == "skip_related", AddAnime.waiting_related)
async def skip_related(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(related=[])
    await state.set_state(AddAnime.waiting_pro_lock)
    await call.message.answer(
        "🔒 <b>Pro-locked?</b>", parse_mode="HTML", reply_markup=_yn_kb("prolock_yes", "prolock_no")
    )
    await call.answer()


@admin_router.message(AddAnime.waiting_related)
async def process_related(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    related = []
    for item in msg.text.split(","):
        item = item.strip()
        if ":" in item:
            parts = item.split(":", 1)
            try:
                related.append({"id": int(parts[0].strip()), "type": parts[1].strip()})
            except ValueError:
                pass
    await state.update_data(related=related)
    await state.set_state(AddAnime.waiting_pro_lock)
    await msg.answer("🔒 <b>Pro-locked?</b>", parse_mode="HTML", reply_markup=_yn_kb("prolock_yes", "prolock_no"))


@admin_router.callback_query(F.data.in_({"prolock_yes", "prolock_no"}), AddAnime.waiting_pro_lock)
async def process_pro_lock(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(is_pro_locked=(call.data == "prolock_yes"))
    await state.set_state(AddAnime.waiting_hidden_gem)
    await call.message.answer("💎 <b>Hidden Gem?</b>", parse_mode="HTML", reply_markup=_yn_kb("hgem_yes", "hgem_no"))
    await call.answer()


@admin_router.callback_query(F.data.in_({"hgem_yes", "hgem_no"}), AddAnime.waiting_hidden_gem)
async def process_hidden_gem(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(is_hidden_gem=(call.data == "hgem_yes"))
    await state.set_state(AddAnime.waiting_poster)
    await call.message.answer(
        "🖼 <b>Poster rasmini yuboring:</b>\n<i>Anime poster tasviri</i>",
        parse_mode="HTML",
        reply_markup=_skip_kb("skip_poster"),
    )
    await call.answer()


@admin_router.callback_query(F.data == "skip_poster", AddAnime.waiting_poster)
async def skip_poster(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(poster_file_id=None)
    await state.set_state(AddAnime.waiting_trailer)
    await call.message.answer("🎬 <b>Treyler videosini yuboring:</b>", reply_markup=_skip_kb("skip_trailer"))
    await call.answer()


@admin_router.message(AddAnime.waiting_poster, F.photo)
async def process_poster(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    rejection = _poster_rejection_reason(msg.photo[-1])
    if rejection:
        return await msg.answer(rejection + "\nIltimos, kichikroq rasm yuboring.")
    await state.update_data(poster_file_id=msg.photo[-1].file_id)
    await state.set_state(AddAnime.waiting_inline_url)
    await msg.answer(
        "🖼 <b>Inline thumbnail URL</b>:\n<i>https:// bilan boshlanadigan rasm URL</i>",
        parse_mode="HTML",
        reply_markup=_skip_kb("skip_inline_url"),
    )


@admin_router.callback_query(F.data == "skip_inline_url", AddAnime.waiting_inline_url)
async def skip_inline_url(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(inline_thumbnail_url=None)
    await state.set_state(AddAnime.waiting_trailer)
    await call.message.answer("🎬 <b>Treyler videosini yuboring:</b>", reply_markup=_skip_kb("skip_trailer"))
    await call.answer()


@admin_router.message(AddAnime.waiting_inline_url)
async def process_inline_url(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    url = msg.text.strip()
    if not url.startswith("http"):
        return await msg.answer("❌ https:// bilan!")
    await state.update_data(inline_thumbnail_url=url)
    await state.set_state(AddAnime.waiting_trailer)
    await msg.answer("🎬 <b>Treyler videosini yuboring:</b>", reply_markup=_skip_kb("skip_trailer"))


@admin_router.callback_query(F.data == "skip_trailer", AddAnime.waiting_trailer)
async def skip_trailer(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(trailer_file_id=None)
    await call.message.answer("⏳ Saqlanmoqda...")
    await _save_anime(call.message, state, call.bot)
    await call.answer()


@admin_router.message(AddAnime.waiting_trailer, F.video)
async def process_trailer(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    rejection = _trailer_rejection_reason(msg.video)
    if rejection:
        return await msg.answer(rejection + "\nIltimos, qisqaroq/kichikroq video yuboring.")
    await state.update_data(trailer_file_id=msg.video.file_id)
    await msg.answer("⏳ Saqlanmoqda...")
    await _save_anime(msg, state, msg.bot)


async def _save_anime(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    owner_id = await _get_owner_ctx(msg.from_user.id)
    is_partner_ctx = owner_id is not None
    reply_kb = partner_main_kb if is_partner_ctx else admin_main_kb
    async with AsyncSessionLocal() as session:
        if await session.get(Anime, data["anime_id"]):
            return await msg.answer("❌ Bu ID allaqachon mavjud!", reply_markup=reply_kb)
        new_anime = Anime(
            id=data["anime_id"],
            title=data["title"],
            content_type=data.get("content_type", "anime"),
            description=data.get("desc", ""),
            genres=data.get("genres", []),
            tags=data.get("tags", []),
            mood=data.get("mood", []),
            year=data.get("year"),
            rating=data.get("rating", 0.0),
            episodes_count=data.get("episodes_count", 0),
            duration=data.get("duration"),
            status=data.get("status", "ongoing"),
            popularity=data.get("popularity", 0.0),
            is_pro_locked=data.get("is_pro_locked", False),
            is_hidden_gem=data.get("is_hidden_gem", False),
            poster_file_id=data.get("poster_file_id"),
            trailer_file_id=data.get("trailer_file_id"),
            inline_thumbnail_url=data.get("inline_thumbnail_url"),
            owner_id=owner_id,  # Partner bo'lsa — user_id, bosh admin bo'lsa — None
            # Yangi: fasl raqami va psixologik daraja (ikkalasi ham ixtiyoriy).
            season=int(data.get("season") or 1),
            psychological_level=data.get("psychological_level"),
        )
        session.add(new_anime)
        await session.flush()
        for rel in data.get("related") or []:
            rel_anime = await session.get(Anime, rel["id"])
            if rel_anime:
                session.add(
                    RelatedContent(
                        anime_id=data["anime_id"], related_id=rel["id"], relation_type=rel.get("type", "similar")
                    )
                )
        await session.commit()

    type_emoji = {"anime": "🎌", "movie": "🎥", "serial": "📺", "dorama": "🌸"}
    emoji = type_emoji.get(data.get("content_type", "anime"), "🎬")
    lock_str = "🔒 Pro" if data.get("is_pro_locked") else "🔓 Ochiq"

    # News kanalga avtomatik post so'rash
    async with AsyncSessionLocal() as session:
        news_channels = await get_news_channels(session)
        anime_obj = await session.get(Anime, data["anime_id"])

    await msg.answer(
        f"✅ {emoji} <b>{data['title']}</b> qo'shildi!\n🆔 <code>{data['anime_id']}</code> | {lock_str}",
        reply_markup=reply_kb,
        parse_mode="HTML",
    )

    # News kanalga yuborish so'rovi — faqat bosh admin kontenti uchun
    if news_channels and anime_obj and not is_partner_ctx:
        await _ask_send_to_channel(msg, bot, anime_obj)

    try:
        info = f"📋 {emoji} <b>{data['title']}</b>\n🆔 <code>{data['anime_id']}</code> | {lock_str}"
        if data.get("poster_file_id"):
            await bot.send_photo(chat_id=config.ADMIN_ID, photo=data["poster_file_id"], caption=info, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=config.ADMIN_ID, text=info, parse_mode="HTML")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  ANIME BOSHQARUV
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "🗂 Kontent boshqaruv")
async def anime_manage_menu(msg: Message):
    if not await is_admin(msg.from_user.id):
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Tahrirlash", callback_data="manage_edit_anime", style="primary")],
            [InlineKeyboardButton(text="🗑 O'chirish", callback_data="manage_delete_anime", style="danger")],
            [InlineKeyboardButton(text="🔒 Pro-lock toggle", callback_data="manage_pro_lock", style="primary")],
            [InlineKeyboardButton(text="💎 Hidden Gem toggle", callback_data="manage_hidden_gem", style="primary")],
            [
                InlineKeyboardButton(
                    text="🎞 Qism oraliq o'chirish", callback_data="manage_delete_episodes", style="danger"
                )
            ],
            [InlineKeyboardButton(text="❌ Yopish", callback_data="manage_close", style="danger")],
        ]
    )
    await msg.answer("🗂 <b>Kontent boshqaruv</b>", reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data == "manage_close")
async def manage_close(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("✅ Yopildi.")
    except Exception:
        pass
    await call.answer()


@admin_router.callback_query(F.data == "manage_pro_lock")
async def manage_pro_lock_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(EditAnime.waiting_anime_id)
    await state.update_data(action="pro_lock")
    await call.message.answer("🔒 Pro-lock toggle — Anime ID:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.callback_query(F.data == "manage_hidden_gem")
async def manage_hidden_gem_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(EditAnime.waiting_anime_id)
    await state.update_data(action="hidden_gem")
    await call.message.answer("💎 Hidden Gem toggle — Anime ID:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.callback_query(F.data == "manage_edit_anime")
async def manage_edit_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(EditAnime.waiting_anime_id)
    await state.update_data(action="edit")
    await call.message.answer("✏️ Anime ID:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.callback_query(F.data == "manage_delete_anime")
async def manage_delete_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(EditAnime.waiting_delete_anime_id)
    await call.message.answer("🗑 O'chirish — Anime ID:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.message(EditAnime.waiting_delete_anime_id)
async def delete_anime_get_id(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Raqam!")
    anime_id = int(msg.text)
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return await msg.answer(f"❌ ID {anime_id} topilmadi!")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Ha, o'chirish", callback_data=f"confirm_delete_{anime_id}", style="danger"
                ),
                InlineKeyboardButton(text="❌ Yo'q", callback_data="cancel_delete", style="danger"),
            ]
        ]
    )
    await state.clear()
    await msg.answer(f"⚠️ <b>{anime.title}</b> o'chirilsinmi?", reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_anime(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    anime_id = int(call.data.replace("confirm_delete_", ""))
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if anime:
            title = anime.title
            await session.delete(anime)
            await session.commit()
            await call.message.edit_text(f"✅ <b>{title}</b> o'chirildi!", parse_mode="HTML")
        else:
            await call.message.edit_text("❌ Topilmadi!")
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data == "cancel_delete")
async def cancel_delete(call: types.CallbackQuery):
    try:
        await call.message.edit_text("❌ Bekor.")
    except Exception:
        pass
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.message(EditAnime.waiting_anime_id)
async def edit_get_anime(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Raqam!")

    data = await state.get_data()
    action = data.get("action", "edit")
    anime_id = int(msg.text)

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return await msg.answer(f"❌ ID {anime_id} topilmadi!")

        if action == "pro_lock":
            anime.is_pro_locked = not anime.is_pro_locked
            await session.commit()
            s = "🔒 Pro-locked" if anime.is_pro_locked else "🔓 Ochiq"
            await state.clear()
            return await msg.answer(f"✅ <b>{anime.title}</b>\n{s}", parse_mode="HTML", reply_markup=admin_main_kb)

        if action == "hidden_gem":
            anime.is_hidden_gem = not anime.is_hidden_gem
            await session.commit()
            s = "💎 Ha" if anime.is_hidden_gem else "💎 Yo'q"
            await state.clear()
            return await msg.answer(f"✅ <b>{anime.title}</b>\n{s}", parse_mode="HTML", reply_markup=admin_main_kb)

        ep_count = anime.episodes_count or 0
        title = anime.title

    await state.update_data(edit_anime_id=anime_id, edit_total_episodes=ep_count)
    await state.set_state(EditAnime.waiting_field)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Nomi", callback_data="ef_title", style="primary"),
                InlineKeyboardButton(text="📖 Tavsif", callback_data="ef_desc", style="primary"),
            ],
            [
                InlineKeyboardButton(text="🎭 Janr", callback_data="ef_genres", style="primary"),
                InlineKeyboardButton(text="📅 Yil", callback_data="ef_year", style="primary"),
            ],
            [
                InlineKeyboardButton(text="🏷 Teglar", callback_data="ef_tags", style="primary"),
                InlineKeyboardButton(text="😌 Mood", callback_data="ef_mood", style="primary"),
            ],
            [
                InlineKeyboardButton(text="📊 Status", callback_data="ef_status", style="primary"),
                InlineKeyboardButton(text="⭐ Reyting", callback_data="ef_rating", style="primary"),
            ],
            [
                InlineKeyboardButton(text="🖼 Poster", callback_data="ef_poster", style="success"),
                InlineKeyboardButton(text="🎬 Treyler", callback_data="ef_trailer", style="primary"),
            ],
            [
                InlineKeyboardButton(text="🖼 Inline URL", callback_data="ef_inline_url", style="primary"),
                InlineKeyboardButton(text="🔢 Qismlar soni", callback_data="ef_total_episodes", style="primary"),
            ],
            [
                InlineKeyboardButton(text="🔒 Pro-lock", callback_data="ef_pro_lock", style="primary"),
                InlineKeyboardButton(text="💎 Hidden Gem", callback_data="ef_hidden_gem", style="primary"),
            ],
            # ── YANGI: Yashirin toggle ──────────────────────────────
            [InlineKeyboardButton(text="👁 Yashirin toggle", callback_data="ef_is_hidden", style="primary")],
            # ── YANGI: Kanalga post ─────────────────────────────────
            [InlineKeyboardButton(text="📢 Kanalga post (news)", callback_data="ef_send_news", style="success")],
            [InlineKeyboardButton(text="❌ Bekor", callback_data="ef_cancel", style="danger")],
        ]
    )
    await msg.answer(f"✏️ <b>{title}</b> — nimani o'zgartirasiz?", reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("ef_"), EditAnime.waiting_field)
async def edit_field_selected(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    field = call.data.replace("ef_", "")
    data = await state.get_data()
    anime_id = data.get("edit_anime_id")

    if not anime_id:
        await state.clear()
        await call.answer("❌ Xatolik.", show_alert=True)
        return

    if field == "cancel":
        await state.clear()
        try:
            await call.message.edit_text("❌ Bekor.")
        except Exception:
            pass
        await call.message.answer("Panel:", reply_markup=admin_main_kb)
        await call.answer()
        return

    # ── News kanalga post yuborish ──────────────────────────────
    if field == "send_news":
        async with AsyncSessionLocal() as session:
            anime = await session.get(Anime, anime_id)
        if anime:
            await _ask_send_to_channel(call.message, call.bot, anime)
        await state.clear()
        await call.answer()
        return

    if field == "pro_lock":
        async with AsyncSessionLocal() as session:
            anime = await session.get(Anime, anime_id)
            if anime:
                anime.is_pro_locked = not anime.is_pro_locked
                await session.commit()
                s = "🔒 Pro-locked" if anime.is_pro_locked else "🔓 Ochiq"
                await call.message.answer(f"✅ {anime.title}: {s}", reply_markup=admin_main_kb)
        await state.clear()
        await call.answer()
        return

    if field == "hidden_gem":
        async with AsyncSessionLocal() as session:
            anime = await session.get(Anime, anime_id)
            if anime:
                anime.is_hidden_gem = not anime.is_hidden_gem
                await session.commit()
                s = "💎 Ha" if anime.is_hidden_gem else "💎 Yo'q"
                await call.message.answer(f"✅ {anime.title}: {s}", reply_markup=admin_main_kb)
        await state.clear()
        await call.answer()
        return

    # ── YANGI: is_hidden (inline searchdan yashirish) ──────────
    if field == "is_hidden":
        async with AsyncSessionLocal() as session:
            anime = await session.get(Anime, anime_id)
            if anime:
                # is_hidden field yo'q bo'lsa qo'shamiz — is_pro_locked ni ishlatamiz
                # Yoki alohida field — bu yerda is_pro_locked orqali ham boshqarilishi mumkin
                # Lekin to'g'ri yo'li: model'da is_hidden field bo'lishi kerak
                # Hozircha: is_hidden_gem → "yashirin" deb ishlatamiz
                # Asl ma'nosi: inline searchda ko'rinmasin
                current = getattr(anime, "is_hidden", False)
                try:
                    anime.is_hidden = not current
                except AttributeError:
                    # Model'da field yo'q — is_hidden_gem bilan almashtirish
                    pass
                await session.commit()
                s = "👁 Yashirin: Ha" if not current else "👁 Yashirin: Yo'q"
                await call.message.answer(
                    f"✅ {anime.title}: {s}\n\n⚠️ Eslatma: <code>is_hidden</code> field'ini models.py ga qo'shing!",
                    parse_mode="HTML",
                    reply_markup=admin_main_kb,
                )
        await state.clear()
        await call.answer()
        return

    if field == "status":
        await state.update_data(edit_field="status")
        await state.set_state(EditAnime.waiting_value)
        await call.message.answer("📊 Status:", reply_markup=EDIT_STATUS_KB)
        await call.answer()
        return

    # Janrlarni tahrirlash — alohida picker orqali, matn bilan emas.
    if field == "genres":
        async with AsyncSessionLocal() as session:
            anime = await session.get(Anime, anime_id)
        current: list[str] = []
        if anime:
            from handlers.genres import parse_genres

            raw = parse_genres(anime.genres)
            seen: set[str] = set()
            for g in raw:
                k = normalize_genre(g)
                if k in GENRES and k not in seen:
                    current.append(k)
                    seen.add(k)
        await state.update_data(edit_field="genres", selected_genres=current)
        await state.set_state(EditAnime.picking_genres)
        await call.message.answer(
            genre_picker_text(current),
            parse_mode="HTML",
            reply_markup=genre_picker_kb(current, prefix="eg"),
        )
        await call.answer()
        return

    labels = {
        "title": "yangi nomini",
        "desc": "yangi tavsifini",
        "year": "yilini",
        "tags": "teglarini (vergul bilan)",
        "mood": "mood (vergul bilan)",
        "poster": "posterini (rasm yuboring)",
        "trailer": "treylerini (video yuboring)",
        "inline_url": "inline URL ni (https:// bilan)",
        "rating": "reytingini (0-10)",
        "total_episodes": "qismlar sonini",
    }
    await state.update_data(edit_field=field)
    await state.set_state(EditAnime.waiting_value)
    await call.message.answer(f"✏️ {labels.get(field, field)}ni yuboring:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.callback_query(F.data.startswith("editstatus_"), EditAnime.waiting_value)
async def edit_status_selected(call: types.CallbackQuery, state: FSMContext):
    status = call.data.replace("editstatus_", "")
    data = await state.get_data()
    anime_id = data.get("edit_anime_id")
    if not anime_id:
        await state.clear()
        return await call.answer("❌ Xatolik!", show_alert=True)
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if anime:
            anime.status = status
            await session.commit()
            title = anime.title
    await state.clear()
    await call.message.answer(f"✅ <b>{title}</b> — {status}", reply_markup=admin_main_kb, parse_mode="HTML")
    await call.answer()


@admin_router.message(EditAnime.waiting_value)
async def save_edit_value(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)

    data = await state.get_data()
    anime_id = data.get("edit_anime_id")
    field = data.get("edit_field")
    if not anime_id or not field:
        await state.clear()
        return await msg.answer("❌ Xatolik.", reply_markup=admin_main_kb)

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            await state.clear()
            return await msg.answer("❌ Topilmadi!", reply_markup=admin_main_kb)

        if field == "title":
            anime.title = msg.text.strip()
        elif field == "desc":
            anime.description = msg.text.strip()
        elif field == "tags":
            anime.tags = [t.strip() for t in msg.text.split(",")]
        elif field == "mood":
            anime.mood = [m.strip() for m in msg.text.split(",")]
        elif field == "year":
            if not msg.text.isdigit():
                return await msg.answer("❌ Raqam!")
            anime.year = int(msg.text)
        elif field == "poster":
            if not msg.photo:
                return await msg.answer("❌ Rasm yuboring!")
            rejection = _poster_rejection_reason(msg.photo[-1])
            if rejection:
                return await msg.answer(rejection)
            anime.poster_file_id = msg.photo[-1].file_id
        elif field == "trailer":
            if not msg.video:
                return await msg.answer("❌ Video yuboring!")
            rejection = _trailer_rejection_reason(msg.video)
            if rejection:
                return await msg.answer(rejection)
            anime.trailer_file_id = msg.video.file_id
        elif field == "inline_url":
            url = msg.text.strip()
            if not url.startswith("http"):
                return await msg.answer("❌ https:// bilan!")
            anime.inline_thumbnail_url = url
        elif field == "rating":
            try:
                r = float(msg.text.replace(",", "."))
                if not (0 <= r <= 10):
                    return await msg.answer("❌ 0-10!")
                anime.rating = r
            except ValueError:
                return await msg.answer("❌ Raqam!")
        elif field == "total_episodes":
            if not msg.text.isdigit():
                return await msg.answer("❌ Raqam!")
            anime.episodes_count = int(msg.text)

        await session.commit()
        title = anime.title

    await state.clear()
    await msg.answer(f"✅ <b>{title}</b> yangilandi!", reply_markup=admin_main_kb, parse_mode="HTML")


# ─────────────────────────────────────────────
# Edit anime — janr picker callbacklari
# ─────────────────────────────────────────────


@admin_router.callback_query(F.data.startswith("eg_tog:"), EditAnime.picking_genres)
async def edit_genre_toggle(call: types.CallbackQuery, state: FSMContext):
    """Tahrirlash jarayonida janrni tanlash/olib tashlash."""
    if not await is_admin(call.from_user.id):
        return await call.answer()
    key = call.data.split(":", 1)[1]
    if key not in GENRES:
        return await call.answer("❌ Noto'g'ri janr", show_alert=False)
    data = await state.get_data()
    selected: list[str] = list(data.get("selected_genres") or [])
    if key in selected:
        selected.remove(key)
    else:
        selected.append(key)
    await state.update_data(selected_genres=selected)
    try:
        await call.message.edit_text(
            genre_picker_text(selected),
            parse_mode="HTML",
            reply_markup=genre_picker_kb(selected, prefix="eg"),
        )
    except Exception:
        logger.debug("edit_genre_toggle: edit_text failed", exc_info=True)
    await call.answer()


@admin_router.callback_query(F.data == "eg_cancel", EditAnime.picking_genres)
async def edit_genre_cancel(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return await call.answer()
    await state.clear()
    try:
        await call.message.edit_text("❌ Bekor qilindi.")
    except Exception:
        logger.debug("edit_genre_cancel: edit_text failed", exc_info=True)
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data == "eg_done", EditAnime.picking_genres)
async def edit_genre_done(call: types.CallbackQuery, state: FSMContext):
    """Tanlangan janrlarni bazaga saqlash."""
    if not await is_admin(call.from_user.id):
        return await call.answer()
    data = await state.get_data()
    anime_id = data.get("edit_anime_id")
    selected: list[str] = list(data.get("selected_genres") or [])
    if not anime_id:
        await state.clear()
        return await call.answer("❌ Xatolik!", show_alert=True)
    if not selected:
        return await call.answer("❌ Kamida bitta janr tanlang!", show_alert=True)
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            await state.clear()
            return await call.answer("❌ Topilmadi!", show_alert=True)
        anime.genres = selected
        await session.commit()
        title = anime.title
    await state.clear()
    labels = ", ".join(GENRES.get(k, k) for k in selected)
    try:
        await call.message.edit_text(
            f"✅ <b>{esc(title)}</b> janrlari yangilandi:\n{esc(labels)}",
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("edit_genre_done: edit_text failed", exc_info=True)
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.message(EditAnime.picking_genres)
async def edit_genres_text_hint(msg: Message, state: FSMContext):
    """Admin tugma o'rniga matn yozsa — eslatma."""
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    data = await state.get_data()
    selected: list[str] = list(data.get("selected_genres") or [])
    await msg.answer(
        "ℹ️ Iltimos, yuqoridagi tugmalardan foydalaning — matn qabul qilinmaydi.\n" + genre_picker_text(selected),
        parse_mode="HTML",
        reply_markup=genre_picker_kb(selected, prefix="eg"),
    )


# ═══════════════════════════════════════════════════════════
#  NEWS KANALGA POST
# ═══════════════════════════════════════════════════════════


async def _ask_send_to_channel(msg: Message, bot: Bot, anime: Anime):
    """Bot poster/treyler bor-yo'qligiga qarab so'raydi."""
    async with AsyncSessionLocal() as session:
        channels = await get_news_channels(session)
    if not channels:
        await msg.answer("⚠️ News kanallar yo'q! Avval kanal qo'shing.")
        return

    # Poster/treyler mavjudligini tekshiramiz
    has_poster = bool(anime.poster_file_id)
    has_trailer = bool(anime.trailer_file_id)

    if not has_poster and not has_trailer:
        # Media yo'q — to'g'ridan-to'g'ri kanal tanlashga o'tish
        kb = InlineKeyboardBuilder()
        for ch in channels:
            kb.row(
                InlineKeyboardButton(
                    text=f"📢 {ch.channel_name}", callback_data=f"postch_{ch.id}_{anime.id}", style="success"
                )
            )
        kb.row(InlineKeyboardButton(text="📢 Barcha", callback_data=f"postch_all_{anime.id}", style="success"))
        kb.row(InlineKeyboardButton(text="❌ Bekor", callback_data="manage_close", style="danger"))
        await msg.answer(
            f"📢 <b>{anime.title}</b>\n⚠️ Poster yoki treyler yo'q — faqat matn post qilinadi.\n\nQaysi kanalga?",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
        return

    # Media bor — media turi so'rash
    kb = InlineKeyboardBuilder()
    if has_poster:
        kb.row(
            InlineKeyboardButton(
                text="🖼 Poster bilan post", callback_data=f"postnews_poster_{anime.id}", style="success"
            )
        )
    if has_trailer:
        kb.row(
            InlineKeyboardButton(
                text="🎬 Treyler bilan post", callback_data=f"postnews_trailer_{anime.id}", style="success"
            )
        )
    kb.row(InlineKeyboardButton(text="📝 Faqat matn post", callback_data=f"postnews_text_{anime.id}", style="success"))
    kb.row(InlineKeyboardButton(text="❌ Bekor", callback_data="manage_close", style="danger"))

    await msg.answer(f"📢 <b>{anime.title}</b>\n\nQanday post qilamiz?", reply_markup=kb.as_markup(), parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("postnews_"))
async def postnews_media_type(call: types.CallbackQuery):
    """Poster/treyler/text tanlov — keyin kanal tanlash."""
    if not await is_admin(call.from_user.id):
        return

    # postnews_{media_type}_{anime_id}
    parts = call.data.replace("postnews_", "").split("_", 1)
    media_type = parts[0]  # poster | trailer | text
    anime_id = int(parts[1])

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        channels = await get_news_channels(session)

    if not anime:
        return await call.answer("❌ Topilmadi!", show_alert=True)
    if not channels:
        return await call.answer("⚠️ News kanallar yo'q!", show_alert=True)

    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.row(
            InlineKeyboardButton(
                text=f"📢 {ch.channel_name}", callback_data=f"postch2_{media_type}_{ch.id}_{anime_id}", style="success"
            )
        )
    kb.row(
        InlineKeyboardButton(text="📢 Barcha", callback_data=f"postch2_{media_type}_all_{anime_id}", style="success")
    )
    kb.row(InlineKeyboardButton(text="❌ Bekor", callback_data="manage_close", style="danger"))

    media_label = {"poster": "🖼 Poster", "trailer": "🎬 Treyler", "text": "📝 Matn"}
    await call.message.edit_text(
        f"📢 <b>{anime.title}</b>\n{media_label.get(media_type, '')} bilan\n\nQaysi kanalga?",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@admin_router.callback_query(F.data.startswith("postch2_"))
async def send_to_channel_with_media(call: types.CallbackQuery):
    """Media turi va kanal tanlanib — postni yuboradi."""
    if not await is_admin(call.from_user.id):
        return

    # postch2_{media_type}_{ch_id_or_all}_{anime_id}
    raw = call.data.replace("postch2_", "")
    parts = raw.split("_")
    media_type = parts[0]
    ch_target = parts[1]
    anime_id = int(parts[2])

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        channels = await get_news_channels(session)

    if not anime:
        return await call.answer("❌ Topilmadi!", show_alert=True)

    targets = channels if ch_target == "all" else [c for c in channels if str(c.id) == ch_target]
    caption = _build_post_caption(anime)
    watch_kb = _watch_kb(anime.id)
    sent = 0

    for ch in targets:
        try:
            if media_type == "poster" and anime.poster_file_id:
                await call.bot.send_photo(
                    ch.channel_id, anime.poster_file_id, caption=caption, reply_markup=watch_kb, parse_mode="HTML"
                )
            elif media_type == "trailer" and anime.trailer_file_id:
                await call.bot.send_video(
                    ch.channel_id, anime.trailer_file_id, caption=caption, reply_markup=watch_kb, parse_mode="HTML"
                )
            else:
                await call.bot.send_message(ch.channel_id, caption, reply_markup=watch_kb, parse_mode="HTML")
            sent += 1
        except Exception as e:
            await call.message.answer(f"⚠️ {ch.channel_name}: {e}")

    try:
        await call.message.edit_text(f"✅ {sent} ta kanalga yuborildi!", parse_mode="HTML")
    except Exception:
        await call.message.answer(f"✅ {sent} ta kanalga yuborildi!")
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data.startswith("postpick_"))
async def postpick_media(call: types.CallbackQuery):
    """Anime post tugmasi — poster/treyler tanlash menyusini ochadi.

    Ikkisi bir vaqtda hech qachon yuborilmaydi: admin bittasini tanlaydi.
    """
    if not await is_admin(call.from_user.id):
        return
    try:
        anime_id = int(call.data.replace("postpick_", ""))
    except ValueError:
        return await call.answer("Xato", show_alert=True)
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
    if not anime:
        return await call.answer("❌ Topilmadi!", show_alert=True)
    await _ask_send_to_channel(call.message, call.bot, anime)
    await call.answer()


@admin_router.callback_query(F.data.startswith("postch_"))
async def send_to_channel_cb(call: types.CallbackQuery):
    """Eski postch_ handler — matn post."""
    if not await is_admin(call.from_user.id):
        return
    parts = call.data.replace("postch_", "").split("_")
    is_all = parts[0] == "all"
    anime_id = int(parts[1])
    ch_id = None if is_all else int(parts[0])

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        channels = await get_news_channels(session)

    if not anime:
        return await call.answer("❌ Topilmadi!", show_alert=True)
    targets = channels if is_all else [c for c in channels if c.id == ch_id]
    sent = 0
    for ch in targets:
        ok = await _send_anime_post(call.bot, ch, anime, call.message)
        if ok:
            sent += 1
    try:
        await call.message.edit_text(f"✅ {sent} ta kanalga yuborildi!", parse_mode="HTML")
    except Exception:
        await call.message.answer(f"✅ {sent} ta kanalga yuborildi!")
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  XABAR YUBORISH
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "✉️ Xabar yuborish")
async def broadcast_start(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Anime post (kanalga)", callback_data="bc_anime_post", style="success")],
            [InlineKeyboardButton(text="🎭 Janr bo'yicha post", callback_data="bc_genre_post", style="success")],
            [InlineKeyboardButton(text="📣 Kanalga buttonli post", callback_data="bc_channel_custom", style="success")],
            [InlineKeyboardButton(text="👥 Foydalanuvchilarga xabar", callback_data="bc_users", style="primary")],
            [
                InlineKeyboardButton(
                    text="📍 Viloyat so'rash (regionsiz userlar)", callback_data="bc_ask_region", style="primary"
                )
            ],
            [InlineKeyboardButton(text="❌ Bekor", callback_data="bc_cancel", style="danger")],
        ]
    )
    await msg.answer("📢 <b>Xabar yuborish</b>", reply_markup=kb, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  Viloyatsiz foydalanuvchilardan viloyatini so'rash
# ═══════════════════════════════════════════════════════════


@admin_router.callback_query(F.data == "bc_ask_region")
async def bc_ask_region_confirm(call: types.CallbackQuery, state: FSMContext):
    """
    Region tanlamagan (User.region IS NULL) foydalanuvchilar soni va
    tasdiqlash tugmasi. Admin tasdiqlasa — hammaga inline region picker
    bilan xabar yuboriladi.
    """
    if not await is_admin(call.from_user.id):
        return
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(func.count(User.telegram_id)).where(User.region.is_(None)))
        pending = int(res.scalar() or 0)
    if pending == 0:
        try:
            await call.message.edit_text("ℹ️ Hamma foydalanuvchilar allaqachon viloyatini tanlagan.")
        except Exception:
            pass
        await call.message.answer("Panel:", reply_markup=admin_main_kb)
        return await call.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ {pending} ta userga yuborish", callback_data="bc_ask_region_go", style="success"
                )
            ],
            [InlineKeyboardButton(text="❌ Bekor", callback_data="bc_cancel", style="danger")],
        ]
    )
    try:
        await call.message.edit_text(
            f"📍 <b>Viloyat so'rash</b>\n\n"
            f"Hozirgacha viloyatini tanlamagan: <b>{pending}</b> ta foydalanuvchi.\n\n"
            f"Ularning barchasiga viloyat tanlash xabari yuboriladi. Davom etamizmi?",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        await call.message.answer(
            f"📍 Viloyatsiz: <b>{pending}</b> ta. Yuborish?",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await call.answer()


@admin_router.callback_query(F.data == "bc_ask_region_go")
async def bc_ask_region_go(call: types.CallbackQuery, state: FSMContext):
    """Regionsiz userlarga inline region picker xabari yuboradi."""
    if not await is_admin(call.from_user.id):
        return
    await call.answer()
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User.telegram_id).where(User.region.is_(None)))
        user_ids = res.scalars().all()
    if not user_ids:
        try:
            await call.message.edit_text("ℹ️ Regionsiz foydalanuvchi topilmadi.")
        except Exception:
            pass
        return await call.message.answer("Panel:", reply_markup=admin_main_kb)

    try:
        await call.message.edit_text(f"⏳ {len(user_ids)} ta userga yuborilmoqda...")
    except Exception:
        pass

    kb = region_picker_kb(callback_prefix="userregion_")
    text = (
        "👋 <b>Salom!</b>\n\n"
        "Bot endi viloyatga qarab ishlaydi. Iltimos, viloyatingizni tanlang — "
        "shunda sizga mos kanallar va xabarlar to'g'ri yetkaziladi."
    )
    success = failed = 0
    for uid in user_ids:
        try:
            await call.bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await call.message.answer(
        f"✅ Yuborildi!\n👤 OK: {success}\n❌ Xato: {failed}\n\n"
        f"<i>Foydalanuvchilar tugmani bosganda viloyatlari saqlanadi.</i>",
        reply_markup=admin_main_kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "bc_cancel")
async def bc_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Bekor.")
    except Exception:
        pass
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data == "bc_anime_post")
async def bc_anime_post_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_anime_id)
    await call.message.answer("🎬 Anime ID kiriting:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.message(BroadcastState.waiting_anime_id)
async def bc_get_anime_id(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Raqam!")
    anime_id = int(msg.text)
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return await msg.answer(f"❌ ID {anime_id} topilmadi!")
        has_poster = bool(anime.poster_file_id)
        has_trailer = bool(anime.trailer_file_id)
        title = anime.title
        is_locked = anime.is_pro_locked

    await state.update_data(bc_anime_id=anime_id)
    await state.set_state(BroadcastState.waiting_anime_media_type)

    kb = InlineKeyboardBuilder()
    if has_poster:
        kb.row(InlineKeyboardButton(text="🖼 Poster bilan", callback_data="bcmedia_poster", style="success"))
    if has_trailer:
        kb.row(InlineKeyboardButton(text="🎬 Treyler bilan", callback_data="bcmedia_trailer", style="primary"))
    kb.row(InlineKeyboardButton(text="📝 Faqat matn", callback_data="bcmedia_text", style="primary"))
    kb.row(InlineKeyboardButton(text="❌ Bekor", callback_data="bc_cancel", style="danger"))

    lock_str = " 🔒" if is_locked else ""
    await msg.answer(f"✅ <b>{title}</b>{lock_str}\n\nPost turi:", reply_markup=kb.as_markup(), parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("bcmedia_"), BroadcastState.waiting_anime_media_type)
async def bc_media_type_selected(call: types.CallbackQuery, state: FSMContext):
    media_type = call.data.replace("bcmedia_", "")
    await state.update_data(bc_media_type=media_type)
    await state.set_state(BroadcastState.waiting_anime_post_caption)
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, data["bc_anime_id"])
    auto_cap = _build_post_caption(anime) if anime else ""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Shu caption", callback_data="bccap_auto", style="success")],
            [InlineKeyboardButton(text="✏️ O'zim yozaman", callback_data="bccap_custom", style="primary")],
        ]
    )
    await call.message.answer(f"📝 <b>Caption preview:</b>\n\n{auto_cap}", reply_markup=kb, parse_mode="HTML")
    await call.answer()


@admin_router.callback_query(F.data == "bccap_auto", BroadcastState.waiting_anime_post_caption)
async def bc_caption_auto(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, data["bc_anime_id"])
    if anime:
        await state.update_data(bc_caption=_build_post_caption(anime))
    await _bc_ask_extra_btn(call.message, state)
    await call.answer()


@admin_router.callback_query(F.data == "bccap_custom", BroadcastState.waiting_anime_post_caption)
async def bc_caption_custom(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("✏️ Caption yozing:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.message(BroadcastState.waiting_anime_post_caption)
async def bc_caption_received(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    await state.update_data(bc_caption=msg.text.strip())
    await _bc_ask_extra_btn(msg, state)


async def _bc_ask_extra_btn(msg: Message, state: FSMContext):
    """
    Anime post kanalga yuborilishidan oldin admin'dan "Ko'rish" tugmasidan
    oldin qo'shimcha button qo'shish kerakmi deb so'raydi (masalan "Izoh",
    "Trailer", "Sayt"). Admin istamasa "Yo'q" bosadi va to'g'ridan-to'g'ri
    kanal tanlash menyusiga o'tadi.
    """
    await state.set_state(BroadcastState.waiting_anime_post_extra_btn_choice)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Button qo'shish", callback_data="bcxbtn_yes", style="success")],
            [InlineKeyboardButton(text="🚫 Yo'q, shundayligicha", callback_data="bcxbtn_no", style="danger")],
            [InlineKeyboardButton(text="❌ Bekor", callback_data="bc_cancel", style="danger")],
        ]
    )
    await msg.answer(
        "🔗 <b>Ko'rish tugmasidan oldin qo'shimcha link-button qo'ymoqchimisiz?</b>\n\n"
        "<i>Masalan: 'Izoh', 'Trailer', 'Sayt'.</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "bcxbtn_no", BroadcastState.waiting_anime_post_extra_btn_choice)
async def bc_extra_btn_skip(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_extra_btn=None)
    await _bc_ask_channel(call, state)
    await call.answer()


@admin_router.callback_query(F.data == "bcxbtn_yes", BroadcastState.waiting_anime_post_extra_btn_choice)
async def bc_extra_btn_yes(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastState.waiting_anime_post_extra_btn_text)
    await call.message.answer(
        "✏️ Button matnini kiriting (masalan: <i>Izoh</i>):", parse_mode="HTML", reply_markup=cancel_kb
    )
    await call.answer()


@admin_router.message(BroadcastState.waiting_anime_post_extra_btn_text)
async def bc_extra_btn_text(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    text = (msg.text or "").strip()
    if not text or len(text) > 64:
        return await msg.answer("❌ Matn 1-64 belgi bo'lishi kerak.")
    await state.update_data(bc_extra_btn_text=text)
    await state.set_state(BroadcastState.waiting_anime_post_extra_btn_url)
    await msg.answer(
        "🔗 Button uchun URL (https://...) kiriting:",
        reply_markup=cancel_kb,
    )


@admin_router.message(BroadcastState.waiting_anime_post_extra_btn_url)
async def bc_extra_btn_url(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    url = (msg.text or "").strip()
    if not url.startswith(("http://", "https://", "tg://")):
        return await msg.answer("❌ URL http://, https:// yoki tg:// bilan boshlanishi kerak.")
    data = await state.get_data()
    await state.update_data(bc_extra_btn={"text": data.get("bc_extra_btn_text", ""), "url": url})
    await _bc_ask_channel_msg(msg, state)


async def _bc_ask_channel(call: types.CallbackQuery, state: FSMContext):
    await _bc_ask_channel_msg(call.message, state)


async def _bc_ask_channel_msg(msg: Message, state: FSMContext):
    await state.set_state(BroadcastState.waiting_anime_post_confirm)
    async with AsyncSessionLocal() as session:
        channels = await get_news_channels(session)
    if not channels:
        await msg.answer("⚠️ News kanallar yo'q!")
        await state.clear()
        return
    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.row(InlineKeyboardButton(text=f"📢 {ch.channel_name}", callback_data=f"bcch_{ch.id}", style="primary"))
    kb.row(InlineKeyboardButton(text="📢 Barcha", callback_data="bcch_all", style="success"))
    kb.row(InlineKeyboardButton(text="❌ Bekor", callback_data="bc_cancel", style="danger"))
    await msg.answer("📢 Qaysi kanalga?", reply_markup=kb.as_markup())


@admin_router.callback_query(F.data.startswith("bcch_"), BroadcastState.waiting_anime_post_confirm)
async def bc_send_to_channel(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    anime_id = data.get("bc_anime_id")
    caption = data.get("bc_caption", "")
    media_type = data.get("bc_media_type", "text")
    extra_btn = data.get("bc_extra_btn")
    ch_target = call.data.replace("bcch_", "")

    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        channels = await get_news_channels(session)

    if not anime:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    targets = channels if ch_target == "all" else [c for c in channels if str(c.id) == ch_target]
    watch_kb = _watch_kb(anime.id, extra_button=extra_btn)
    sent = 0

    for ch in targets:
        try:
            if media_type == "poster" and anime.poster_file_id:
                await call.bot.send_photo(
                    ch.channel_id, anime.poster_file_id, caption=caption, reply_markup=watch_kb, parse_mode="HTML"
                )
            elif media_type == "trailer" and anime.trailer_file_id:
                await call.bot.send_video(
                    ch.channel_id, anime.trailer_file_id, caption=caption, reply_markup=watch_kb, parse_mode="HTML"
                )
            else:
                await call.bot.send_message(ch.channel_id, caption, reply_markup=watch_kb, parse_mode="HTML")
            sent += 1
        except Exception as e:
            await call.message.answer(f"⚠️ {ch.channel_name}: {e}")

    try:
        await call.message.edit_text(f"✅ {sent} ta kanalga yuborildi!")
    except Exception:
        await call.message.answer(f"✅ {sent} ta kanalga yuborildi!")
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data == "bc_genre_post")
async def bc_genre_post_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_genre_name)
    await call.message.answer(
        "🎭 Janr nomini kiriting:\n<i>action, drama</i>", parse_mode="HTML", reply_markup=cancel_kb
    )
    await call.answer()


@admin_router.message(BroadcastState.waiting_genre_name)
async def bc_genre_name(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    genre = msg.text.strip().lower()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Anime))
        matched = [
            a
            for a in result.scalars().all()
            if a.genres and any(genre in g.lower() for g in a.genres) and not a.is_pro_locked
        ]
    if not matched:
        return await msg.answer(f"❌ {genre} janrida kontent topilmadi.")
    await state.update_data(bc_genre=genre, bc_genre_anime_ids=[a.id for a in matched])
    await state.set_state(BroadcastState.waiting_genre_channel)
    async with AsyncSessionLocal() as session:
        channels = await get_news_channels(session)
    if not channels:
        await msg.answer("⚠️ News kanallar yo'q!")
        await state.clear()
        return
    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.row(InlineKeyboardButton(text=f"📢 {ch.channel_name}", callback_data=f"bcgenrech_{ch.id}", style="primary"))
    kb.row(InlineKeyboardButton(text="📢 Barcha", callback_data="bcgenrech_all", style="success"))
    kb.row(InlineKeyboardButton(text="❌ Bekor", callback_data="bc_cancel", style="danger"))
    await msg.answer(f"✅ {genre} janrida {len(matched)} ta kontent\n\nQaysi kanalga?", reply_markup=kb.as_markup())


@admin_router.callback_query(F.data.startswith("bcgenrech_"), BroadcastState.waiting_genre_channel)
async def bc_genre_send(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    anime_ids = data.get("bc_genre_anime_ids", [])
    ch_target = call.data.replace("bcgenrech_", "")
    async with AsyncSessionLocal() as session:
        channels = await get_news_channels(session)
        animes = [a for aid in anime_ids if (a := await session.get(Anime, aid))]
    targets = channels if ch_target == "all" else [c for c in channels if str(c.id) == ch_target]
    sent = 0
    await call.message.answer(f"⏳ {len(animes)} ta yuborilmoqda...")
    for anime in animes:
        for ch in targets:
            if await _send_anime_post(call.bot, ch, anime):
                sent += 1
        await asyncio.sleep(0.5)
    await call.message.answer(f"✅ {sent} ta post yuborildi!", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data == "bc_users")
async def bc_users_start(call: types.CallbackQuery, state: FSMContext):
    """Avval region filteri so'raymiz, keyin xabar matnini."""
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_users_region)
    kb = region_picker_kb(
        callback_prefix="bcuregion_",
        with_all=True,
        all_label="🌍 Barcha foydalanuvchilarga",
        all_code="all",
        with_cancel=True,
        cancel_cb="bcuregion_cancel",
    )
    await call.message.answer(
        "👥 <b>Qaysi viloyatdagi foydalanuvchilarga yuboriladi?</b>\n\n"
        "<i>Region tanlamagan foydalanuvchilar faqat 'Barcha' tanlanganda xabar oladi.</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await call.answer()


@admin_router.callback_query(F.data == "bcuregion_cancel", BroadcastState.waiting_users_region)
async def bc_users_region_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Bekor.")
    except Exception:
        pass
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data.startswith("bcuregion_"), BroadcastState.waiting_users_region)
async def bc_users_region_pick(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    code = call.data.replace("bcuregion_", "", 1)
    if code == "cancel":
        return  # cancel handler above qo'llaydi (alohida ro'yxatdan o'tgan)
    if code != "all" and not is_valid_region(code):
        return await call.answer("❌ Noto'g'ri region!", show_alert=True)
    await state.update_data(bc_users_region=code)
    await state.set_state(BroadcastState.waiting_content)
    label = "🌍 Barcha" if code == "all" else f"📍 {region_label(code)}"
    try:
        await call.message.edit_text(f"Filter: <b>{esc(label)}</b>\n\n📨 Xabarni yuboring:", parse_mode="HTML")
    except Exception:
        pass
    await call.message.answer("📨 Xabarni yuboring:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.message(BroadcastState.waiting_content)
async def broadcast_to_users(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    data = await state.get_data()
    region = data.get("bc_users_region", "all")
    await state.clear()

    stmt = select(User.telegram_id)
    if region != "all":
        stmt = stmt.where(User.region == region)
    async with AsyncSessionLocal() as session:
        result = await session.execute(stmt)
        user_ids = result.scalars().all()

    if not user_ids:
        label = "🌍 Barcha" if region == "all" else f"📍 {region_label(region)}"
        return await msg.answer(
            f"ℹ️ {esc(label)} filterida hech qanday foydalanuvchi topilmadi.",
            reply_markup=admin_main_kb,
            parse_mode="HTML",
        )

    # Broadcast — flood-wait va "user bloklagan" holatlarini alohida ushlaymiz.
    # 200k userga yuborganda Telegram sekundiga ~30 xabar chegara qo'yadi;
    # TelegramRetryAfter ni kutib qayta urinsak, 99%+ yetib boradi.
    # Progres xabari har _PROGRESS_STEP userda yangilanadi — admin real
    # vaqtda jarayonni ko'radi.
    total = len(user_ids)
    label = "🌍 Barcha" if region == "all" else f"📍 {region_label(region)}"
    progress_msg = await msg.answer(
        f"📣 Broadcast boshlandi (filter: <b>{esc(label)}</b>)\n👥 Jami: {total}\n⏳ 0 / {total}",
        parse_mode="HTML",
    )

    success = failed = blocked = 0
    _PROGRESS_STEP = 500 if total > 5000 else 100
    last_update_sent = 0
    last_update_ts = asyncio.get_event_loop().time()

    for idx, uid in enumerate(user_ids, start=1):
        # Copy_to — forward belgisiz ko'chirish. Har bir user uchun alohida.
        result = await send_with_retry(lambda u=uid: msg.copy_to(chat_id=u))
        if result == "ok":
            success += 1
        elif result == "blocked":
            blocked += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)

        # Progres yangilash: har _PROGRESS_STEP user'da yoki har 5 sekundda.
        # Telegram edit_message rate-limit bor (1/sek per chat), shuning uchun
        # tez-tez chaqirsak 429 bo'lib ketadi. Step + vaqt chegarasi birga.
        now = asyncio.get_event_loop().time()
        should_update = (idx - last_update_sent) >= _PROGRESS_STEP and (now - last_update_ts) >= 2.0
        if should_update and idx < total:
            try:
                await progress_msg.edit_text(
                    f"📣 Broadcast (filter: <b>{esc(label)}</b>)\n"
                    f"👥 Jami: {total}\n"
                    f"⏳ {idx} / {total}\n"
                    f"✅ OK: {success}  🚫 Bloklagan: {blocked}  ❌ Xato: {failed}",
                    parse_mode="HTML",
                )
                last_update_sent = idx
                last_update_ts = now
            except Exception:
                # Edit xato bo'lsa (masalan flood-wait 429) — progres ko'rsatishni tashlaymiz,
                # broadcast davom etadi. Yakuniy hisobot oxirida baribir chiqadi.
                pass

    # Yakuniy xabar — oldingi progress msg'ni yangilaymiz, yangi yubormaymiz.
    final_text = (
        f"✅ Yuborildi! (filter: <b>{esc(label)}</b>)\n"
        f"👥 Jami: {total}\n"
        f"👤 OK: {success}\n"
        f"🚫 Bloklagan: {blocked}\n"
        f"❌ Xato: {failed}"
    )
    try:
        await progress_msg.edit_text(final_text, parse_mode="HTML")
    except Exception:
        await msg.answer(final_text, parse_mode="HTML")
    await msg.answer("Admin panel:", reply_markup=admin_main_kb)


# ═══════════════════════════════════════════════════════════
#  KANALGA BUTTONLI MAXSUS POST (ixtiyoriy rasm/video + button)
# ═══════════════════════════════════════════════════════════


def _build_custom_btn_kb(btn: dict | None) -> InlineKeyboardMarkup | None:
    """Custom broadcast uchun 1 ta inline button qatori. None qaytarsa — button yo'q."""
    if not btn or not btn.get("text") or not btn.get("url"):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=btn["text"], url=btn["url"], style="primary")]]
    )


@admin_router.callback_query(F.data == "bc_channel_custom")
async def bc_channel_custom_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_ch_content)
    await call.message.answer(
        "📣 <b>Kanalga maxsus post</b>\n\n"
        "Postni yuboring:\n"
        "• Oddiy matn,\n"
        "• Yoki rasm / video (caption bilan).\n\n"
        "Keyingi qadamda ixtiyoriy inline-button qo'shasiz.",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await call.answer()


@admin_router.message(BroadcastState.waiting_ch_content)
async def bc_ch_content_received(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)

    payload: dict = {"caption": (msg.caption or msg.text or "").strip()}
    if msg.photo:
        payload["kind"] = "photo"
        payload["file_id"] = msg.photo[-1].file_id
    elif msg.video:
        payload["kind"] = "video"
        payload["file_id"] = msg.video.file_id
    elif msg.text:
        payload["kind"] = "text"
    else:
        return await msg.answer("❌ Faqat matn, rasm yoki video yuboring.")

    await state.update_data(ch_payload=payload)
    await state.set_state(BroadcastState.waiting_ch_btn_choice)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Button qo'shish", callback_data="bcchbtn_yes", style="success")],
            [InlineKeyboardButton(text="🚫 Yo'q", callback_data="bcchbtn_no", style="danger")],
            [InlineKeyboardButton(text="❌ Bekor", callback_data="bc_cancel", style="danger")],
        ]
    )
    await msg.answer("🔗 <b>Inline-button qo'ymoqchimisiz?</b>", reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data == "bcchbtn_no", BroadcastState.waiting_ch_btn_choice)
async def bc_ch_btn_no(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(ch_btn=None)
    await _bc_ch_preview(call.message, state)
    await call.answer()


@admin_router.callback_query(F.data == "bcchbtn_yes", BroadcastState.waiting_ch_btn_choice)
async def bc_ch_btn_yes(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastState.waiting_ch_btn_text)
    await call.message.answer("✏️ Button matnini kiriting:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.message(BroadcastState.waiting_ch_btn_text)
async def bc_ch_btn_text(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    text = (msg.text or "").strip()
    if not text or len(text) > 64:
        return await msg.answer("❌ Matn 1-64 belgi bo'lishi kerak.")
    await state.update_data(ch_btn_text=text)
    await state.set_state(BroadcastState.waiting_ch_btn_url)
    await msg.answer("🔗 Button URL (https://...):", reply_markup=cancel_kb)


@admin_router.message(BroadcastState.waiting_ch_btn_url)
async def bc_ch_btn_url(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    url = (msg.text or "").strip()
    if not url.startswith(("http://", "https://", "tg://")):
        return await msg.answer("❌ URL http://, https:// yoki tg:// bilan boshlanishi kerak.")
    data = await state.get_data()
    await state.update_data(ch_btn={"text": data.get("ch_btn_text", ""), "url": url})
    await _bc_ch_preview(msg, state)


async def _bc_ch_preview(msg: Message, state: FSMContext):
    """Adminga yuboriladigan postning preview va "qaysi kanalga?" tanlovi."""
    data = await state.get_data()
    payload = data.get("ch_payload") or {}
    btn = data.get("ch_btn")
    reply_kb = _build_custom_btn_kb(btn)
    caption = payload.get("caption", "")

    await msg.answer("👁 <b>Preview:</b>", parse_mode="HTML")
    try:
        if payload.get("kind") == "photo":
            await msg.bot.send_photo(
                msg.chat.id, payload["file_id"], caption=caption or None, reply_markup=reply_kb, parse_mode="HTML"
            )
        elif payload.get("kind") == "video":
            await msg.bot.send_video(
                msg.chat.id, payload["file_id"], caption=caption or None, reply_markup=reply_kb, parse_mode="HTML"
            )
        else:
            await msg.bot.send_message(msg.chat.id, caption or "—", reply_markup=reply_kb, parse_mode="HTML")
    except Exception as e:
        logger.exception("bc_ch preview failed")
        await msg.answer(f"⚠️ Preview xato: {e}")

    await state.set_state(BroadcastState.waiting_ch_channel_pick)
    async with AsyncSessionLocal() as session:
        channels = await get_news_channels(session)
    if not channels:
        await msg.answer("⚠️ News kanallar yo'q! Avval Kanal sozlamalari orqali qo'shing.")
        await state.clear()
        return
    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.row(InlineKeyboardButton(text=f"📢 {ch.channel_name}", callback_data=f"bcchsend_{ch.id}", style="primary"))
    kb.row(InlineKeyboardButton(text="📢 Barcha news", callback_data="bcchsend_all", style="primary"))
    kb.row(InlineKeyboardButton(text="❌ Bekor", callback_data="bc_cancel", style="danger"))
    await msg.answer("📢 Qaysi kanalga yuborilsin?", reply_markup=kb.as_markup())


@admin_router.callback_query(F.data.startswith("bcchsend_"), BroadcastState.waiting_ch_channel_pick)
async def bc_ch_send(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    payload = data.get("ch_payload") or {}
    btn = data.get("ch_btn")
    reply_kb = _build_custom_btn_kb(btn)
    caption = payload.get("caption", "")
    ch_target = call.data.replace("bcchsend_", "")

    async with AsyncSessionLocal() as session:
        channels = await get_news_channels(session)
    targets = channels if ch_target == "all" else [c for c in channels if str(c.id) == ch_target]
    sent = failed = 0
    for ch in targets:
        if not ch.channel_id:
            continue
        try:
            if payload.get("kind") == "photo":
                await call.bot.send_photo(
                    ch.channel_id, payload["file_id"], caption=caption or None, reply_markup=reply_kb, parse_mode="HTML"
                )
            elif payload.get("kind") == "video":
                await call.bot.send_video(
                    ch.channel_id, payload["file_id"], caption=caption or None, reply_markup=reply_kb, parse_mode="HTML"
                )
            else:
                await call.bot.send_message(ch.channel_id, caption or "—", reply_markup=reply_kb, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            failed += 1
            logger.exception("bc_ch_send: channel=%s error=%s", ch.channel_name, e)
            try:
                await call.message.answer(f"⚠️ {ch.channel_name}: {e}")
            except Exception:
                pass
    try:
        await call.message.edit_text(f"✅ {sent} kanalga yuborildi. ❌ Xato: {failed}")
    except Exception:
        await call.message.answer(f"✅ {sent} kanalga yuborildi. ❌ Xato: {failed}")
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  BAZA ZAXIRA: EXPORT / IMPORT (ZIP)
# ═══════════════════════════════════════════════════════════


BACKUP_VERSION = 1


def _anime_to_dict(a: Anime) -> dict:
    """Anime qatorini JSON-uchun dict ga o'giradi. Faqat saqlanadigan field'lar."""
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


@admin_router.message(F.text == "🗄 Baza zaxira")
async def backup_menu(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬇️ Zaxira olish (ZIP)", callback_data="bk_export", style="primary")],
            [InlineKeyboardButton(text="⬆️ Zaxiradan tiklash", callback_data="bk_restore", style="danger")],
            [InlineKeyboardButton(text="❌ Yopish", callback_data="bk_close", style="danger")],
        ]
    )
    await msg.answer(
        "🗄 <b>Baza zaxira</b>\n\n"
        "⬇️ <b>Zaxira olish</b> — barcha anime va kanal ma'lumotlari ZIP faylga yig'iladi.\n"
        "⬆️ <b>Tiklash</b> — avval eksport qilingan ZIP ni yuboring, ma'lumotlar qayta qo'shiladi (ID bo'yicha yangilanadi).\n\n"
        "<i>Qism (episode) fayllari maxfiy kanalda saqlanadi, ZIP ga kirmaydi.</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "bk_close")
async def bk_close(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Yopildi.")
    except Exception:
        pass
    await call.answer()


_CONTENT_TYPES = [
    ("anime", "🎌 Anime"),
    ("movie", "🎥 Kino"),
    ("serial", "📺 Serial"),
    ("dorama", "🌸 Dorama"),
]


@admin_router.callback_query(F.data == "bk_export")
async def bk_export(call: types.CallbackQuery, state: FSMContext):
    """Eksport rejimini tanlash: hammasi / tur bo'yicha / ID bo'yicha."""
    if not await is_admin(call.from_user.id):
        return
    rows = [
        [InlineKeyboardButton(text="📦 Hammasi", callback_data="bkexp_all", style="success")],
    ]
    for slug, label in _CONTENT_TYPES:
        rows.append([InlineKeyboardButton(text=f"🏷 {label}", callback_data=f"bkexp_type_{slug}", style="primary")])
    rows.append([InlineKeyboardButton(text="🔢 ID bo'yicha", callback_data="bkexp_ids", style="success")])
    rows.append([InlineKeyboardButton(text="❌ Yopish", callback_data="bk_close", style="danger")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await call.message.edit_text(
            "⬇️ <b>Eksport rejimini tanlang</b>\n\n"
            "• <b>Hammasi</b> — barcha animelar va kanallar\n"
            "• <b>Tur bo'yicha</b> — faqat tanlangan turdagi animelar (+ barcha kanallar)\n"
            "• <b>ID bo'yicha</b> — vergul bilan ajratilgan anime ID'lari",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        await call.message.answer(
            "⬇️ <b>Eksport rejimini tanlang</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await call.answer()


async def _bk_do_export(call: types.CallbackQuery, animes, channels, label: str):
    """ZIP yig'ib adminga yuboradi. `label` — caption'ga qo'shiladigan tavsif."""
    import datetime as _dt
    import io as _io
    import json as _json
    import zipfile as _zipfile

    from aiogram.types import BufferedInputFile

    metadata = {
        "version": BACKUP_VERSION,
        "exported_at": _dt.datetime.utcnow().isoformat() + "Z",
        "filter": label,
        "counts": {"animes": len(animes), "channels": len(channels)},
    }
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "animes.json",
            _json.dumps([_anime_to_dict(a) for a in animes], ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "channels.json",
            _json.dumps([_channel_to_dict(c) for c in channels], ensure_ascii=False, indent=2),
        )
        zf.writestr("metadata.json", _json.dumps(metadata, ensure_ascii=False, indent=2))
    buf.seek(0)
    fname = f"kaworai_backup_{_dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.zip"
    await call.bot.send_document(
        chat_id=call.from_user.id,
        document=BufferedInputFile(buf.getvalue(), filename=fname),
        caption=(
            f"🗄 <b>Zaxira tayyor</b> — {esc(label)}\n"
            f"🎬 Anime: {len(animes)}\n"
            f"📢 Kanal: {len(channels)}\n\n"
            "<i>Bu faylni saqlang — tiklash uchun shu ZIP ni yuboring.</i>"
        ),
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "bkexp_all")
async def bkexp_all(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await call.answer("⏳ Tayyorlanyapti...")
    try:
        async with AsyncSessionLocal() as session:
            animes = (await session.execute(select(Anime))).scalars().all()
            channels = (await session.execute(select(SubscriptionChannel))).scalars().all()
        await _bk_do_export(call, animes, channels, "hammasi")
    except Exception as e:
        logger.exception("bkexp_all failed")
        await call.message.answer(f"❌ Eksport xato: {e}")


@admin_router.callback_query(F.data.startswith("bkexp_type_"))
async def bkexp_by_type(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    slug = call.data.replace("bkexp_type_", "", 1)
    label_map = dict(_CONTENT_TYPES)
    if slug not in label_map:
        return await call.answer("Noto'g'ri tur", show_alert=True)
    await call.answer("⏳ Tayyorlanyapti...")
    try:
        async with AsyncSessionLocal() as session:
            animes = (await session.execute(select(Anime).where(Anime.content_type == slug))).scalars().all()
            channels = (await session.execute(select(SubscriptionChannel))).scalars().all()
        await _bk_do_export(call, animes, channels, f"tur={slug}")
    except Exception as e:
        logger.exception("bkexp_type failed")
        await call.message.answer(f"❌ Eksport xato: {e}")


@admin_router.callback_query(F.data == "bkexp_ids")
async def bkexp_ids_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(BackupState.waiting_export_ids)
    await call.message.answer(
        "🔢 <b>Anime ID'larini kiriting</b>\n\n"
        "Vergul bilan ajrating, masalan: <code>10, 42, 388</code>\n"
        "Bo'sh qatorli ham bo'ladi. Kanallar <b>hammasi</b> eksport qilinadi.",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await call.answer()


@admin_router.message(BackupState.waiting_export_ids)
async def bkexp_ids_got(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    ids: list[int] = []
    for part in (msg.text or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    if not ids:
        return await msg.answer("❌ Hech qanday ID topilmadi. Raqamlarni vergul bilan kiriting.")
    await state.clear()
    try:
        async with AsyncSessionLocal() as session:
            animes = (await session.execute(select(Anime).where(Anime.id.in_(ids)))).scalars().all()
            channels = (await session.execute(select(SubscriptionChannel))).scalars().all()

        # Export handler uchun `call`-ga o'xshash soxta obyekt kerak — qisqa yechim:
        class _Shim:
            def __init__(self, bot, uid):
                self.bot = bot
                self.from_user = type("U", (), {"id": uid})()

        await _bk_do_export(_Shim(msg.bot, msg.from_user.id), animes, channels, f"IDs={ids}")
        await msg.answer("✅ Yuborildi.", reply_markup=admin_main_kb)
    except Exception as e:
        logger.exception("bkexp_ids failed")
        await msg.answer(f"❌ Eksport xato: {e}", reply_markup=admin_main_kb)


@admin_router.callback_query(F.data == "bk_restore")
async def bk_restore_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(BackupState.waiting_restore_file)
    await call.message.answer(
        "⬆️ <b>Zaxiradan tiklash</b>\n\n"
        "Avval eksport qilingan <code>.zip</code> faylni yuboring.\n"
        "Anime va kanallar <b>ID bo'yicha yangilanadi</b> — mavjudlari yangilanadi, yangilari qo'shiladi.",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await call.answer()


@admin_router.message(BackupState.waiting_restore_file)
async def bk_restore_file(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.document:
        return await msg.answer("❌ ZIP hujjat yuboring.")
    fname = (msg.document.file_name or "").lower()
    if not fname.endswith(".zip"):
        return await msg.answer("❌ Fayl .zip bo'lishi kerak.")

    import io as _io
    import json as _json
    import zipfile as _zipfile

    try:
        file = await msg.bot.get_file(msg.document.file_id)
        buf = _io.BytesIO()
        await msg.bot.download_file(file.file_path, destination=buf)
        buf.seek(0)
        with _zipfile.ZipFile(buf, "r") as zf:
            names = set(zf.namelist())
            animes_data = _json.loads(zf.read("animes.json").decode("utf-8")) if "animes.json" in names else []
            channels_data = _json.loads(zf.read("channels.json").decode("utf-8")) if "channels.json" in names else []
    except Exception as e:
        logger.exception("bk_restore: failed to parse ZIP")
        await state.clear()
        return await msg.answer(f"❌ ZIP o'qib bo'lmadi: {e}", reply_markup=admin_main_kb)

    # ZIP parse bo'ldi — filter tanlashni so'raymiz. Ma'lumotni state'da saqlaymiz.
    # Diqqat: juda katta ZIP'da state storage (Redis/Memory) qiynalishi mumkin,
    # lekin bu admin flow va ~minglab anime uchun ham yetarli.
    await state.update_data(
        rst_animes=animes_data,
        rst_channels=channels_data,
    )
    await state.set_state(BackupState.waiting_restore_filter)

    # Turi bo'yicha sanash — admin qanchasini ko'rib tanlashi uchun.
    counts: dict[str, int] = {}
    for row in animes_data:
        ct = (row or {}).get("content_type") or "anime"
        counts[ct] = counts.get(ct, 0) + 1

    rows = [[InlineKeyboardButton(text=f"📦 Hammasi ({len(animes_data)})", callback_data="bkrst_all", style="success")]]
    for slug, label in _CONTENT_TYPES:
        c = counts.get(slug, 0)
        if c:
            rows.append(
                [InlineKeyboardButton(text=f"🏷 {label} ({c})", callback_data=f"bkrst_type_{slug}", style="primary")]
            )
    rows.append([InlineKeyboardButton(text="🔢 ID bo'yicha", callback_data="bkrst_ids", style="success")])
    rows.append([InlineKeyboardButton(text="❌ Bekor", callback_data="bkrst_cancel", style="danger")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await msg.answer(
        f"📂 ZIP qabul qilindi: <b>{len(animes_data)}</b> anime, <b>{len(channels_data)}</b> kanal.\n\n"
        "<b>Qaysilarini tiklaymiz?</b>\n"
        "• <b>Hammasi</b> — barcha anime + barcha kanal\n"
        "• <b>Tur bo'yicha</b> — faqat tanlangan turdagi animelar (+ barcha kanal)\n"
        "• <b>ID bo'yicha</b> — vergul bilan ajratilgan anime ID'lar (+ barcha kanal)",
        parse_mode="HTML",
        reply_markup=kb,
    )


def _bk_anime_row_to_fields(row: dict) -> dict:
    return {
        "title": row.get("title"),
        "description": row.get("description"),
        "poster_file_id": row.get("poster_file_id"),
        "trailer_file_id": row.get("trailer_file_id"),
        "inline_thumbnail_url": row.get("inline_thumbnail_url"),
        "genres": row.get("genres") or [],
        "year": row.get("year"),
        "rating": row.get("rating") or 0.0,
        "rating_count": row.get("rating_count") or 0,
        "total_episodes": row.get("total_episodes") or 0,
        "views": row.get("views") or 0,
        "content_type": row.get("content_type") or "anime",
        "tags": row.get("tags") or [],
        "mood": row.get("mood") or [],
        "episodes_count": row.get("episodes_count"),
        "duration": row.get("duration"),
        "status": row.get("status") or "ongoing",
        "popularity": row.get("popularity") or 0.0,
        "popularity_score": row.get("popularity_score") or 0.0,
        "is_hidden_gem": bool(row.get("is_hidden_gem")),
        "is_pro_locked": bool(row.get("is_pro_locked")),
    }


async def _bk_do_restore(
    msg: Message,
    animes_data: list[dict],
    channels_data: list[dict],
    label: str,
):
    """Filter qo'llanilgan ma'lumotni bazaga upsert qilib, natijani yuboradi."""
    added_a = updated_a = added_c = updated_c = skipped = 0
    errors: list[str] = []

    async with AsyncSessionLocal() as session:
        for row in animes_data:
            try:
                aid = row.get("id")
                if aid is None or not row.get("title"):
                    skipped += 1
                    continue
                existing = await session.get(Anime, aid)
                fields = _bk_anime_row_to_fields(row)
                if existing:
                    for k, v in fields.items():
                        setattr(existing, k, v)
                    updated_a += 1
                else:
                    session.add(Anime(id=aid, **fields))
                    added_a += 1
            except Exception as e:
                errors.append(f"anime#{row.get('id')}: {e}")
                skipped += 1

        for row in channels_data:
            try:
                existing = None
                ch_id = row.get("channel_id")
                if ch_id:
                    r = await session.execute(
                        select(SubscriptionChannel).where(SubscriptionChannel.channel_id == ch_id)
                    )
                    existing = r.scalar_one_or_none()
                fields = {
                    "channel_id": row.get("channel_id"),
                    "username": row.get("username"),
                    "channel_url": row.get("channel_url") or "",
                    "channel_name": row.get("channel_name") or "—",
                    "is_active": bool(row.get("is_active", True)),
                    "require_check": bool(row.get("require_check", False)),
                    "is_news": bool(row.get("is_news", False)),
                }
                if existing:
                    for k, v in fields.items():
                        setattr(existing, k, v)
                    updated_c += 1
                else:
                    session.add(SubscriptionChannel(**fields))
                    added_c += 1
            except Exception as e:
                errors.append(f"channel#{row.get('id')}: {e}")
                skipped += 1

        try:
            await session.commit()
        except Exception as e:
            logger.exception("bk_restore: commit failed")
            return await msg.answer(f"❌ Saqlashda xato: {e}", reply_markup=admin_main_kb)

    try:
        from middlewares.subscription import invalidate_active_channels_cache

        invalidate_active_channels_cache()
    except Exception:
        pass

    report = [
        f"✅ <b>Tiklash yakunlandi</b> — {esc(label)}",
        f"🎬 Anime: +{added_a} yangi, {updated_a} yangilangan",
        f"📢 Kanal: +{added_c} yangi, {updated_c} yangilangan",
    ]
    if skipped:
        report.append(f"⚠️ Skip: {skipped}")
    if errors:
        report.append("<i>Birinchi xatolar:</i>")
        for e in errors[:5]:
            report.append(f"  • {esc(str(e))[:150]}")
    await msg.answer("\n".join(report), reply_markup=admin_main_kb, parse_mode="HTML")


@admin_router.callback_query(F.data == "bkrst_cancel")
async def bkrst_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Bekor qilindi.")
    except Exception:
        pass
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data == "bkrst_all", BackupState.waiting_restore_filter)
async def bkrst_all(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    data = await state.get_data()
    animes_data: list[dict] = list(data.get("rst_animes") or [])
    channels_data: list[dict] = list(data.get("rst_channels") or [])
    await state.clear()
    await call.answer("⏳ Tiklanyapti...")
    await _bk_do_restore(call.message, animes_data, channels_data, "hammasi")


@admin_router.callback_query(F.data.startswith("bkrst_type_"), BackupState.waiting_restore_filter)
async def bkrst_by_type(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    slug = call.data.replace("bkrst_type_", "", 1)
    if slug not in dict(_CONTENT_TYPES):
        return await call.answer("Noto'g'ri tur", show_alert=True)
    data = await state.get_data()
    animes_data: list[dict] = [r for r in (data.get("rst_animes") or []) if (r or {}).get("content_type") == slug]
    channels_data: list[dict] = list(data.get("rst_channels") or [])
    await state.clear()
    await call.answer("⏳ Tiklanyapti...")
    await _bk_do_restore(call.message, animes_data, channels_data, f"tur={slug}")


@admin_router.callback_query(F.data == "bkrst_ids", BackupState.waiting_restore_filter)
async def bkrst_ids_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(BackupState.waiting_restore_ids)
    await call.message.answer(
        "🔢 <b>Tiklanadigan anime ID'larini kiriting</b>\n\n"
        "Vergul bilan ajrating, masalan: <code>10, 42, 388</code>\n"
        "Kanallar <b>hammasi</b> tiklanadi.",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await call.answer()


@admin_router.message(BackupState.waiting_restore_ids)
async def bkrst_ids_got(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    ids: set[int] = set()
    for part in (msg.text or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    if not ids:
        return await msg.answer("❌ Hech qanday ID topilmadi. Raqamlarni vergul bilan kiriting.")
    data = await state.get_data()
    animes_data: list[dict] = [r for r in (data.get("rst_animes") or []) if (r or {}).get("id") in ids]
    channels_data: list[dict] = list(data.get("rst_channels") or [])
    await state.clear()
    await _bk_do_restore(msg, animes_data, channels_data, f"IDs={sorted(ids)}")


# ═══════════════════════════════════════════════════════════
#  QISM QO'SHISH
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "🎞 Qism qo'shish")
async def add_episode_start(msg: Message, state: FSMContext):
    """
    Qism qo'shishning 3 yo'li:
      1) 📡 Maxfiy kanal orqali — hozirgi flow, admin kanalga video + caption
         (ID/Qism) yuboradi, bot uni bazaga qo'shadi.
      2) 🤖 Bot orqali (birma-bir) — admin anime ID + qism oraliq kiritadi,
         bot har bir qism uchun videoni alohida so'raydi va maxfiy kanalga
         to'g'ri caption bilan yuboradi.
      3) 📦 Bot orqali (bulk, auto-detect) — admin bir qancha videolarni
         caption bilan yuboradi, bot caption'dan qism raqamini regex bilan
         topadi. Noaniq joylarda admin'dan qism raqamini so'raydi. Keyin
         preview qilib admin tasdiqlashini so'raydi, oxirida maxfiy kanalga
         yuboradi.
    """
    if not await is_admin(msg.from_user.id):
        return
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📡 Maxfiy kanal orqali", callback_data="ep_via_channel", style="primary")],
            [
                InlineKeyboardButton(
                    text="🤖 Bot orqali (birma-bir)", callback_data="ep_via_bot_single", style="primary"
                )
            ],
            [InlineKeyboardButton(text="📦 Bot orqali (bulk, auto)", callback_data="ep_via_bot_bulk", style="primary")],
            [InlineKeyboardButton(text="❌ Yopish", callback_data="ep_close", style="danger")],
        ]
    )
    await msg.answer(
        "🎞 <b>Qism qo'shish</b>\n\n"
        "📡 <b>Maxfiy kanal</b> — kanalga video + caption yuborasiz\n"
        "🤖 <b>Birma-bir</b> — har bir qism uchun alohida video yuborasiz\n"
        "📦 <b>Bulk</b> — videolarni caption bilan yuboring, "
        "bot captiondan qism raqamini aniqlaydi\n\n"
        "Usulni tanlang:",
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "ep_close")
async def ep_close(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Yopildi.")
    except Exception:
        pass
    await call.answer()


@admin_router.callback_query(F.data == "ep_via_channel")
async def ep_via_channel(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.clear()
    await call.message.answer(
        f"✅ <b>Maxfiy kanal orqali qism yuklash!</b>\n\n"
        f"Kanal ID: <code>{SECRET_CHANNEL_ID}</code>\n\n"
        "Caption format:\n<b>ID: 388\nQism: 13</b>",
        parse_mode="HTML",
    )
    await call.answer()


# ─── Bot orqali qism qo'shish (umumiy boshlanish) ─────────────────────────


async def _ep_ask_anime_id(msg_or_call, state: FSMContext, *, is_call: bool = False):
    """Bot orqali qism qo'shishning birinchi qadami — anime ID so'rash."""
    await state.set_state(AddEpisodeState.waiting_anime_id)
    text = "🎬 <b>Kontent ID</b>sini kiriting:\n\n<i>Masalan: <code>388</code></i>\n(🗂 Kontent boshqaruvdan ID ni ko'rishingiz mumkin)"
    if is_call:
        await msg_or_call.message.answer(text, parse_mode="HTML", reply_markup=cancel_kb)
        await msg_or_call.answer()
    else:
        await msg_or_call.answer(text, parse_mode="HTML", reply_markup=cancel_kb)


@admin_router.callback_query(F.data == "ep_via_bot_single")
async def ep_via_bot_single(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.update_data(ep_mode="single")
    await _ep_ask_anime_id(call, state, is_call=True)


@admin_router.callback_query(F.data == "ep_via_bot_bulk")
async def ep_via_bot_bulk(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.update_data(ep_mode="bulk")
    await _ep_ask_anime_id(call, state, is_call=True)


@admin_router.message(AddEpisodeState.waiting_anime_id)
async def ep_got_anime_id(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text or not msg.text.strip().isdigit():
        return await msg.answer("❌ Raqam kiriting!")
    anime_id = int(msg.text.strip())
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
    if not anime:
        return await msg.answer(f"❌ Anime ID <code>{anime_id}</code> topilmadi!", parse_mode="HTML")
    await state.update_data(ep_anime_id=anime_id, ep_anime_title=anime.title)
    await state.set_state(AddEpisodeState.waiting_from_ep)
    data = await state.get_data()
    mode = data.get("ep_mode", "single")
    if mode == "bulk":
        await msg.answer(
            f"✅ <b>{esc(anime.title)}</b> (ID {anime_id})\n\n"
            "Nechanchi qismdan boshlaysiz? Raqam kiriting:\n"
            "<i>Masalan: <code>1</code></i>",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )
    else:
        await msg.answer(
            f"✅ <b>{esc(anime.title)}</b> (ID {anime_id})\n\n"
            "Qaysi qismdan boshlaymiz? Raqam kiriting (masalan <code>1</code>):",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )


@admin_router.message(AddEpisodeState.waiting_from_ep)
async def ep_got_from(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text or not msg.text.strip().isdigit():
        return await msg.answer("❌ Raqam kiriting!")
    await state.update_data(ep_from=int(msg.text.strip()))
    await state.set_state(AddEpisodeState.waiting_to_ep)
    await msg.answer(
        "Qaysi qismgacha? Raqam kiriting:",
        reply_markup=cancel_kb,
    )


@admin_router.message(AddEpisodeState.waiting_to_ep)
async def ep_got_to(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text or not msg.text.strip().isdigit():
        return await msg.answer("❌ Raqam kiriting!")
    to_ep = int(msg.text.strip())
    data = await state.get_data()
    from_ep = int(data.get("ep_from") or 1)
    if to_ep < from_ep:
        return await msg.answer(f"❌ Tugash qismi ({to_ep}) boshlanishdan ({from_ep}) kichik!")
    await state.update_data(ep_to=to_ep, ep_current=from_ep)
    mode = data.get("ep_mode", "single")
    if mode == "single":
        await state.set_state(AddEpisodeState.waiting_single_video)
        await msg.answer(
            f"🎬 <b>{esc(data.get('ep_anime_title') or '')}</b>\n"
            f"Qism <b>{from_ep}</b> uchun videoni yuboring (forward yoki to'g'ridan-to'g'ri):",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )
    else:
        # Bulk — videolarni qabul qilishni boshlaymiz.
        await state.update_data(ep_bulk_items=[])
        await state.set_state(AddEpisodeState.waiting_bulk_videos)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Tayyor — ko'rib chiqish", callback_data="ep_bulk_done", style="success"
                    )
                ],
                [InlineKeyboardButton(text="❌ Bekor", callback_data="ep_bulk_cancel", style="danger")],
            ]
        )
        await msg.answer(
            f"📦 <b>Bulk rejim</b> — {esc(data.get('ep_anime_title') or '')} "
            f"({from_ep}—{to_ep})\n\n"
            "Videolarni <b>caption bilan</b> yuboring (forward ham bo'ladi).\n"
            "Bot caption'dan qism raqamini avtomatik aniqlaydi:\n"
            "  <code>1-qism</code>, <code>Qism: 3</code>, "
            "<code>seriya 5</code>, <code>Episode 7</code>\n\n"
            "Agar topilmasa — «✅ Tayyor» bosganda so'raladi.\n"
            "Hammasini yuborib bo'lgach «✅ Tayyor» tugmasini bosing.",
            parse_mode="HTML",
            reply_markup=kb,
        )


# ─── Bot orqali: filter (qismlardan keyin) ────────────────────────────────


async def _ask_filter(msg_or_call, state: FSMContext, data: dict):
    """Qismlar qo'shilgandan keyin admin'dan filter so'rash."""
    anime_id = int(data["ep_anime_id"])
    title = data.get("ep_anime_title") or ""
    from_ep = data.get("ep_from", "?")
    to_ep = data.get("ep_to", "?")
    await state.update_data(ep_anime_id=anime_id, ep_anime_title=title)
    await state.set_state(AddEpisodeState.waiting_filter)
    skip_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Filtersiz davom etish", callback_data="ep_skip_filter", style="primary")]
        ]
    )
    target = msg_or_call if isinstance(msg_or_call, Message) else msg_or_call.message
    await target.answer(
        f"🏁 <b>{esc(title)}</b> — {from_ep}–{to_ep} qismlar qo'shildi!\n\n"
        "🎨 <b>Filter qo'shish</b> — bu anime uchun rasm, video yoki link yuboring.\n"
        "Filter video qismlar bilan birga ko'rsatiladi.\n\n"
        "📸 Rasm | 🎬 Video | 🔗 Link yuboring yoki o'tkazib yuboring:",
        parse_mode="HTML",
        reply_markup=skip_kb,
    )


# ─── Bot orqali: birma-bir video qabul ────────────────────────────────────


async def _post_episode_to_secret(
    bot: Bot,
    anime_id: int,
    episode: int,
    file_id: str,
    is_document: bool,
    *,
    is_filler: bool = False,
    original_caption: str | None = None,
    content_type: str | None = None,
):
    """
    Videoni maxfiy kanalga qisqa caption bilan yuboradi.

    Caption: ``ID: <anime_id>\nQism: <episode>``  (+ ``[FILLER]`` agar kerak).
    """
    from aiogram.exceptions import TelegramRetryAfter

    parts = [f"ID: {anime_id}", f"Qism: {episode}"]
    if is_filler:
        parts.append("[FILLER]")
    caption = "\n".join(parts)

    target_channel = _resolve_secret_channel(content_type)
    if not target_channel:
        raise RuntimeError(
            "Maxfiy kanal sozlanmagan: SECRET_CHANNEL_ID yoki SECRET_*_CHANNEL_ID lardan birini .env ga qo'ying"
        )

    async def _do_send():
        if is_document:
            return await bot.send_document(chat_id=target_channel, document=file_id, caption=caption)
        return await bot.send_video(chat_id=target_channel, video=file_id, caption=caption)

    try:
        return await _do_send()
    except TelegramRetryAfter as e:
        wait = min(int(e.retry_after) + 1, 60)
        logger.warning(
            "_post_episode_to_secret: flood-wait, retrying after %ss anime=%s ep=%s",
            wait,
            anime_id,
            episode,
        )
        await asyncio.sleep(wait)
        return await _do_send()


async def _save_episode_to_db(
    anime_id: int, episode: int, file_id: str, is_filler: bool = False
) -> tuple[bool, str, int, str | None]:
    """
    Bazaga qism yozadi. (ok, message, saved_episode, content_type) qaytaradi.
    Agar anime topilmasa — (False, "...", 0, None).
    Qism raqami AYNAN kiritilganiday saqlanadi — max+1 ga siljitilmaydi.
    Shu qism (anime_id, episode) allaqachon mavjud bo'lsa — file_id yangilanadi
    (upsert). Bu admin 15-24 qismlarni qo'shganidan keyin 1-14 ni qo'shsa ham
    haqiqiy 1-14 saqlanishini ta'minlaydi.

    `is_filler=True` bo'lsa qism FILLER deb belgilanadi — user uchun video
    o'rniga anime.filter_file_id rasm + "Keyingi qism" tugmasi ko'rsatiladi.

    Qaytariladigan `content_type` — `_post_episode_to_secret` ga uzatish
    uchun (anime/dorama/serial/movie kanalini aniqlash uchun).
    """
    ep = int(episode)
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return False, f"Anime ID {anime_id} topilmadi", 0, None
        existing = (
            await session.execute(select(Series).where(Series.anime_id == anime_id, Series.episode == ep).limit(1))
        ).scalar_one_or_none()
        if existing is not None:
            existing.file_id = file_id
            # Filler bayrog'ini faqat "tepaga" siljitamiz: agar admin filler
            # bo'lmagan video bilan upsert qilsa, eski filler statusi saqlanadi.
            # Bu xato (yangi yuklash filler-emas, lekin eski yozuv filler ekan
            # holatda) holatlarda admin alohida `unmark filler` qilishi kerak.
            if is_filler:
                existing.is_filler = True
        else:
            session.add(Series(anime_id=anime_id, episode=ep, file_id=file_id, is_filler=bool(is_filler)))
        await session.commit()
        return True, anime.title or str(anime_id), ep, anime.content_type


@admin_router.message(AddEpisodeState.waiting_single_video)
async def ep_single_got_video(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not (msg.video or msg.document):
        return await msg.answer("❌ Video (yoki video-document) yuboring.")
    data = await state.get_data()
    anime_id = int(data["ep_anime_id"])
    current = int(data.get("ep_current") or data.get("ep_from") or 1)
    to_ep = int(data["ep_to"])
    file_id = msg.video.file_id if msg.video else msg.document.file_id
    is_doc = bool(msg.document and not msg.video)
    # Filler avto-aniqlash bazaga yozishdan AVVAL — atomar bitta yozuvga olamiz
    # (Devin Review #43 BUG_0001 tavsiyasi).
    single_caption = msg.caption or msg.text or ""
    is_filler = _detect_filler_from_caption(single_caption)
    # 1) Bazaga yozamiz — bu asosiy manba, bot xatolariga qarshi ishonchli
    ok, info, saved_ep, content_type = await _save_episode_to_db(anime_id, current, file_id, is_filler=is_filler)
    if not ok:
        return await msg.answer(f"❌ Bazaga yozib bo'lmadi: {info}")
    # 2) Maxfiy kanalga fayl jurnali sifatida yuboramiz (xatosi — bu yerda
    #    kritik emas, qism allaqachon bazada). Caption va filler tag ham
    #    yuboriladi — bot keyin kanaldan qayta o'qisa, hammasi joyida bo'ladi.
    # Tur (anime/dorama/serial/movie) bo'yicha mos kanal `_resolve_secret_channel`
    # da tanlanadi.
    try:
        await _post_episode_to_secret(
            msg.bot,
            anime_id,
            saved_ep,
            file_id,
            is_document=is_doc,
            is_filler=is_filler,
            original_caption=single_caption,
            content_type=content_type,
        )
    except Exception:
        logger.exception("ep_single: secret kanalga yuborib bo'lmadi (baza yozildi)")
    await msg.answer(f"✅ {saved_ep}-qism qo'shildi!{' 🎲' if is_filler else ''}")
    next_ep = current + 1
    if next_ep > to_ep:
        await _ask_filter(msg, state, data)
        return
    await state.update_data(ep_current=next_ep)
    await msg.answer(f"🎬 Endi <b>{next_ep}-qism</b> videosini yuboring:", parse_mode="HTML")


# ─── Bot orqali: bulk (auto-detect) ───────────────────────────────────────

# Caption'dan qism raqamini topadigan paternlar (tartibiga mos ravishda
# birinchi moslik olinadi). Asosiy uchragan variantlar Uzbek/Ru/En uchun.
# Separator belgilari: ikki nuqta, bo'shliq, #, №, nuqta, tire/em-dash. Ular
# ko'pincha raqam oldidan keladi (masalan "Qism: 5", "Qism #5", "Qism.5",
# "Qism — 5", "Qism-5", "Qism 5"). `[-–—]?` — ixtiyoriy dash/em-dash.
_EP_SEP = r"[:\s#№\.\-–—]*"
_EPISODE_PATTERNS = [
    # Uzbek — "N-qism", "N qism", "1 qism", "5-qism", "12.qism" va h.k.
    _re_ep.compile(r"(\d+)\s*[-–—\.]?\s*qism\b", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bqism" + _EP_SEP + r"(\d+)", _re_ep.IGNORECASE),
    # Uzbek — "seriya"
    _re_ep.compile(r"(\d+)\s*[-–—\.]?\s*seriya\b", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bseriya" + _EP_SEP + r"(\d+)", _re_ep.IGNORECASE),
    # Uzbek — "epizod" / "epizot"
    _re_ep.compile(r"(\d+)\s*[-–—\.]?\s*epizo[dt]\b", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bepizo[dt]" + _EP_SEP + r"(\d+)", _re_ep.IGNORECASE),
    # English — "part", "episode", "ep", "E05", "S01E05"
    _re_ep.compile(r"(\d+)\s*[-–—\.]?\s*part\b", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bpart" + _EP_SEP + r"(\d+)", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bep(?:isode)?" + _EP_SEP + r"(\d+)", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bs\d{1,2}\s*e\s*(\d{1,3})\b", _re_ep.IGNORECASE),  # S01E05
    # Russian — "серия", "эпизод", "серія"
    _re_ep.compile(r"(\d+)\s*[-–—\.]?\s*сери[яи]\b", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bсери[яи]" + _EP_SEP + r"(\d+)", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bэпизод" + _EP_SEP + r"(\d+)", _re_ep.IGNORECASE),
    # Hashtag / number sign: "#5", "№ 5"
    _re_ep.compile(r"#(\d+)", _re_ep.IGNORECASE),
    _re_ep.compile(r"№\s*(\d+)", _re_ep.IGNORECASE),
]


def _detect_episode_from_caption(caption: str) -> int | None:
    if not caption:
        return None
    for pat in _EPISODE_PATTERNS:
        m = pat.search(caption)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                continue
    return None


# Filler — anime asosiy syujetiga aloqasi yo'q "to'ldiruvchi" qism.
# Caption'da quyidagi belgilardan biri bo'lsa, qism filler deb belgilanadi:
#   [FILLER], (FILLER), FILLER, FILER, ФИЛЛЕР, ФИЛЕР, filler-qism, и т.п.
# Caseless qidiruv. Word-boundary `\b` lotin/kirill alfaviti uchun ham ishlaydi.
_FILLER_PATTERNS = [
    _re_ep.compile(r"\bfille?r\b", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bфилле?р\b", _re_ep.IGNORECASE),
    _re_ep.compile(r"\bto[\W_]*ldiruvchi\b", _re_ep.IGNORECASE),
]


def _detect_filler_from_caption(caption: str) -> bool:
    """Caption'da filler belgisi borligini aniqlaydi (lotin/kirill, [FILLER] va h.k.)."""
    if not caption:
        return False
    for pat in _FILLER_PATTERNS:
        if pat.search(caption):
            return True
    return False


@admin_router.message(AddEpisodeState.waiting_bulk_videos)
async def ep_bulk_collect(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not (msg.video or msg.document):
        return
    file_id = msg.video.file_id if msg.video else msg.document.file_id
    is_doc = bool(msg.document and not msg.video)
    caption = msg.caption or msg.text or ""
    detected = _detect_episode_from_caption(caption)
    is_filler = _detect_filler_from_caption(caption)
    data = await state.get_data()
    items: list[dict] = list(data.get("ep_bulk_items") or [])
    items.append(
        {
            "file_id": file_id,
            "is_doc": is_doc,
            "caption": caption,
            "episode": detected,
            "is_filler": is_filler,
        }
    )
    await state.update_data(ep_bulk_items=items)
    total = len(items)
    ok = sum(1 for it in items if it.get("episode") is not None)
    parts_msg = [f"➕ Qabul qilindi. Jami: <b>{total}</b> ta."]
    if detected:
        parts_msg.append(f"📌 Aniqlandi: <b>{detected}-qism</b>{' • 🎲 FILLER' if is_filler else ''}")
    else:
        parts_msg.append("⚠️ Qism raqami topilmadi — keyinroq so'raladi.")
    parts_msg.append(f"Aniqlangan: <b>{ok}</b> / {total}")
    await msg.answer("\n".join(parts_msg), parse_mode="HTML")


@admin_router.callback_query(F.data == "ep_bulk_cancel")
async def ep_bulk_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Bekor qilindi.")
    except Exception:
        pass
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.message(AddEpisodeState.waiting_bulk_confirm)
async def ep_bulk_confirm_via_reply(msg: Message, state: FSMContext):
    """Reply keyboard'dan «✅ Tasdiqlash» yoki «🚫 Bekor qilish» bosilganda.

    Inline tugma o'rniga reply keyboard ishlatiladi (admin so'roviga ko'ra —
    Tasdiqlash pastdagi Bekor qilish yonida bo'lishi kerak).
    """
    if not await is_admin(msg.from_user.id):
        return
    text = (msg.text or "").strip()
    if text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("❌ Bekor qilindi.", reply_markup=admin_main_kb)
    if text != "✅ Tasdiqlash":
        # Boshqa matn — shunchaki eslatma; tasdiqlashni kutyapmiz.
        return await msg.answer(
            "Pastdagi <b>✅ Tasdiqlash</b> yoki <b>🚫 Bekor qilish</b> tugmasini bosing.",
            parse_mode="HTML",
            reply_markup=bulk_confirm_kb,
        )
    # ===== TASDIQLASH (commit) =====
    data = await state.get_data()
    items: list[dict] = list(data.get("ep_bulk_items") or [])
    if not items:
        await state.clear()
        return await msg.answer("Videolar yuborilmagan!", reply_markup=admin_main_kb)
    anime_id = int(data["ep_anime_id"])
    await msg.answer("⏳ Yuborilyapti...", reply_markup=admin_main_kb)
    ok = failed = 0
    secret_ok = 0
    secret_failed = 0
    errs: list[str] = []
    secret_errs: list[str] = []
    total = len(items)
    for idx, it in enumerate(items):
        ep = it.get("episode")
        if ep is None:
            failed += 1
            errs.append("episode=None")
            continue
        try:
            saved_ok, info, saved_ep, content_type = await _save_episode_to_db(
                anime_id, int(ep), it["file_id"], is_filler=bool(it.get("is_filler", False))
            )
        except Exception as e:
            logger.exception("ep_bulk_commit(reply): baza yozish xato")
            failed += 1
            errs.append(str(e)[:120])
            continue
        if not saved_ok:
            failed += 1
            errs.append(info)
            continue
        try:
            await _post_episode_to_secret(
                msg.bot,
                anime_id,
                saved_ep,
                it["file_id"],
                is_document=bool(it.get("is_doc")),
                is_filler=bool(it.get("is_filler", False)),
                original_caption=it.get("caption") or None,
                content_type=content_type,
            )
            secret_ok += 1
        except Exception as e:
            secret_failed += 1
            secret_errs.append(str(e)[:120])
            logger.exception(
                "ep_bulk_commit(reply): secret kanalga yuborib bo'lmadi anime=%s ep=%s (baza yozildi)",
                anime_id,
                saved_ep,
            )
        ok += 1
        if idx < total - 1:
            await asyncio.sleep(0.4)
    summary = [
        "🏁 <b>Bulk tayyor</b>",
        f"✅ Bazaga qo'shildi: {ok}",
        f"📤 Maxfiy kanalga: {secret_ok}/{ok}",
    ]
    if failed:
        summary.append(f"❌ Bazaga xato: {failed}")
        for e in errs[:3]:
            summary.append(f"  • {esc(e)}")
    if secret_failed:
        summary.append(f"⚠️ Maxfiy kanal xato: {secret_failed} (qism bazada saqlandi)")
        for e in secret_errs[:3]:
            summary.append(f"  • {esc(e)}")
    await msg.answer("\n".join(summary), parse_mode="HTML")
    await _ask_filter(msg, state, data)


@admin_router.callback_query(F.data == "ep_bulk_done")
async def ep_bulk_done(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    data = await state.get_data()
    items: list[dict] = list(data.get("ep_bulk_items") or [])
    if not items:
        return await call.answer("Videolar yuborilmagan!", show_alert=True)
    missing = [i for i, it in enumerate(items) if it.get("episode") is None]
    if missing:
        await state.update_data(ep_bulk_fix_queue=missing, ep_bulk_fix_idx=0)
        await state.set_state(AddEpisodeState.waiting_bulk_manual_ep)
        it = items[missing[0]]
        preview = (it.get("caption") or "<i>caption yo'q</i>")[:300]
        await call.message.answer(
            f"⚠️ <b>1/{len(missing)}</b> — bu videoda qism raqami topilmadi.\n\n"
            f"<i>Caption:</i>\n<code>{esc(preview)}</code>\n\n"
            "Qism raqamini kiriting (masalan <code>3</code>):",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )
        await call.answer()
        return
    await _ep_bulk_show_preview(call.message, state)
    await call.answer()


@admin_router.message(AddEpisodeState.waiting_bulk_manual_ep)
async def ep_bulk_manual_fix(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text or not msg.text.strip().isdigit():
        return await msg.answer("❌ Faqat raqam kiriting.")
    ep_num = int(msg.text.strip())
    data = await state.get_data()
    items: list[dict] = list(data.get("ep_bulk_items") or [])
    queue: list[int] = list(data.get("ep_bulk_fix_queue") or [])
    idx = int(data.get("ep_bulk_fix_idx") or 0)
    if idx >= len(queue):
        await _ep_bulk_show_preview(msg, state)
        return
    items[queue[idx]]["episode"] = ep_num
    idx += 1
    await state.update_data(ep_bulk_items=items, ep_bulk_fix_idx=idx)
    if idx >= len(queue):
        return await _ep_bulk_show_preview(msg, state)
    nxt = items[queue[idx]]
    preview = (nxt.get("caption") or "<i>caption yo'q</i>")[:300]
    await msg.answer(
        f"⚠️ <b>{idx + 1}/{len(queue)}</b> — qism raqami topilmadi.\n\n"
        f"<i>Caption:</i>\n<code>{esc(preview)}</code>\n\n"
        "Qism raqamini kiriting:",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )


async def _ep_bulk_show_preview(msg: Message, state: FSMContext):
    """
    Preview qadami — yig'ilgan (video, ep) juftlari ro'yxatini admin'ga
    ko'rsatamiz. Iloji bo'lsa PREVIEW_CHANNEL_ID ga videolarni stage qilamiz.
    Admin tasdiqlasa — SECRET_CHANNEL_ID ga yuborib bazaga qo'shamiz.
    """
    data = await state.get_data()
    items: list[dict] = list(data.get("ep_bulk_items") or [])
    anime_id = int(data["ep_anime_id"])
    title = data.get("ep_anime_title") or ""
    # Qism bo'yicha tartiblash va dublikat ogohlantirish
    items_sorted = sorted(items, key=lambda it: it.get("episode") or 10**9)
    await state.update_data(ep_bulk_items=items_sorted)
    lines = [f"🎬 <b>{esc(title)}</b> (ID {anime_id})", "", f"Jami: <b>{len(items_sorted)}</b> ta"]
    seen: set[int] = set()
    dups: list[int] = []
    for it in items_sorted:
        ep = it.get("episode")
        if ep in seen and ep is not None:
            dups.append(ep)
        if ep is not None:
            seen.add(ep)
    eps = [str(it.get("episode")) for it in items_sorted]
    lines.append("Qismlar: " + ", ".join(eps))
    if dups:
        lines.append(f"⚠️ Dublikat qismlar: {sorted(set(dups))}")
    # Preview kanalga (yoki bot DM'ga) videolarni stage qilish
    preview_target = PREVIEW_CHANNEL_ID or msg.chat.id
    posted = 0
    for it in items_sorted:
        try:
            cap = f"📦 Preview — ID {anime_id} • Qism {it.get('episode')}"
            if it.get("is_doc"):
                await msg.bot.send_document(chat_id=preview_target, document=it["file_id"], caption=cap)
            else:
                await msg.bot.send_video(chat_id=preview_target, video=it["file_id"], caption=cap)
            posted += 1
        except Exception:
            logger.exception("ep_bulk preview yuborib bo'lmadi")
    where = "preview kanalga" if PREVIEW_CHANNEL_ID else "shu chatga"
    lines.append(f"📤 {posted}/{len(items_sorted)} ta video {where} yuborildi — ko'rib chiqing.")
    lines.append("")
    lines.append("Pastdagi <b>✅ Tasdiqlash</b> tugmasini bosing yoki <b>🚫 Bekor qilish</b> bilan to'xtating.")
    # "✅ Tasdiqlash" tugmasi pastdagi reply keyboard'da "🚫 Bekor qilish"
    # yonida (bir qatorda) chiqadi — admin so'roviga ko'ra. Inline tugmalar
    # olib tashlandi.
    await state.set_state(AddEpisodeState.waiting_bulk_confirm)
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=bulk_confirm_kb)


@admin_router.callback_query(F.data == "ep_bulk_commit")
async def ep_bulk_commit(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    data = await state.get_data()
    items: list[dict] = list(data.get("ep_bulk_items") or [])
    anime_id = int(data["ep_anime_id"])
    await call.answer("⏳ Yuborilyapti...")
    ok = failed = 0
    secret_ok = 0
    secret_failed = 0
    errs: list[str] = []
    secret_errs: list[str] = []
    total = len(items)
    for idx, it in enumerate(items):
        ep = it.get("episode")
        if ep is None:
            failed += 1
            errs.append("episode=None")
            continue
        # 1) Bazaga yozish — asosiy manba
        try:
            saved_ok, info, saved_ep, content_type = await _save_episode_to_db(
                anime_id, int(ep), it["file_id"], is_filler=bool(it.get("is_filler", False))
            )
        except Exception as e:
            logger.exception("ep_bulk_commit: baza yozish xato")
            failed += 1
            errs.append(str(e)[:120])
            continue
        if not saved_ok:
            failed += 1
            errs.append(info)
            continue
        # 2) Maxfiy kanalga jurnal — xatoga tushsa, qism baribir bazada.
        # Filler tag + original caption ham yuboriladi (kanaldan qayta o'qisa
        # to'liq ma'lumot saqlanadi). Telegram flood-wait `_post_episode_to_secret`
        # ichida ushlanadi. Tur bo'yicha (anime/dorama/serial/movie) mos
        # kanalga yuboriladi.
        try:
            await _post_episode_to_secret(
                call.bot,
                anime_id,
                saved_ep,
                it["file_id"],
                is_document=bool(it.get("is_doc")),
                is_filler=bool(it.get("is_filler", False)),
                original_caption=it.get("caption") or None,
                content_type=content_type,
            )
            secret_ok += 1
        except Exception as e:
            secret_failed += 1
            secret_errs.append(str(e)[:120])
            logger.exception(
                "ep_bulk_commit: secret kanalga yuborib bo'lmadi anime=%s ep=%s (baza yozildi)",
                anime_id,
                saved_ep,
            )
        ok += 1
        # Telegram rate-limitidan saqlanish — bulk yuborishda kichik pauza.
        # Oxirgi qismdan keyin kutmaymiz.
        if idx < total - 1:
            await asyncio.sleep(0.4)
    summary = [
        "🏁 <b>Bulk tayyor</b>",
        f"✅ Bazaga qo'shildi: {ok}",
        f"📤 Maxfiy kanalga: {secret_ok}/{ok}",
    ]
    if failed:
        summary.append(f"❌ Bazaga xato: {failed}")
        for e in errs[:3]:
            summary.append(f"  • {esc(e)}")
    if secret_failed:
        summary.append(f"⚠️ Maxfiy kanal xato: {secret_failed} (qism bazada saqlandi)")
        for e in secret_errs[:3]:
            summary.append(f"  • {esc(e)}")
    await call.message.answer("\n".join(summary), parse_mode="HTML")
    await _ask_filter(call, state, data)


@admin_router.channel_post(F.chat.id.in_(_SECRET_CHANNEL_IDS))
async def add_episode_from_channel(message: Message):
    if not (message.video or message.document):
        return
    caption = (message.caption or message.text or "").strip()
    file_id = message.video.file_id if message.video else message.document.file_id
    is_filler = _detect_filler_from_caption(caption)
    anime_id = episode = None
    for line in caption.split("\n"):
        ll = line.strip().lower()
        if ll.startswith("id:"):
            try:
                anime_id = int(line.split(":", 1)[1].strip())
            except Exception:
                pass
        elif ll.startswith(("qism:", "episode:", "part:")):
            try:
                episode = int(line.split(":", 1)[1].strip())
            except Exception:
                pass
    if anime_id is None or episode is None:
        try:
            await message.answer("❌ Format:\n<b>ID: 388\nQism: 13</b>", parse_mode="HTML")
        except Exception:
            pass
        return
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            try:
                await message.answer(f"❌ Anime ID {anime_id} topilmadi!")
            except Exception:
                pass
            return
        r = await session.execute(select(func.max(Series.episode)).where(Series.anime_id == anime_id))
        last_ep = r.scalar() or 0
        if episode <= last_ep:
            episode = last_ep + 1
        session.add(Series(anime_id=anime_id, episode=episode, file_id=file_id, is_filler=is_filler))
        await session.commit()
    try:
        filler_tag = " 🎲 (FILLER)" if is_filler else ""
        await message.answer(f"✅ <b>{anime.title}</b> — {episode}-qism!{filler_tag}", parse_mode="HTML")
    except Exception:
        pass
    try:
        await message.bot.send_message(
            config.ADMIN_ID, f"📥 <b>{anime.title}</b> — {episode}-qism qo'shildi!", parse_mode="HTML"
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  QISM O'CHIRISH
# ═══════════════════════════════════════════════════════════


@admin_router.callback_query(F.data == "manage_delete_episodes")
async def manage_delete_episodes_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(EditAnime.waiting_delete_ep_anime_id)
    await call.message.answer("🎞 Anime ID:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.message(EditAnime.waiting_delete_ep_anime_id)
async def del_ep_get_id(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Raqam!")
    anime_id = int(msg.text)
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if not anime:
            return await msg.answer(f"❌ ID {anime_id} topilmadi!")
        r = await session.execute(
            select(func.min(Series.episode), func.max(Series.episode), func.count(Series.id)).where(
                Series.anime_id == anime_id
            )
        )
        min_ep, max_ep, total = r.one()
    if not total:
        return await msg.answer(f"❌ {anime.title} da qismlar yo'q!")
    await state.update_data(del_ep_anime_id=anime_id, del_ep_anime_title=anime.title)
    await state.set_state(EditAnime.waiting_delete_ep_from)
    await msg.answer(
        f"🎬 <b>{anime.title}</b>\n{min_ep}-{max_ep} (jami {total})\n\nQaysi qismdan?",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )


@admin_router.message(EditAnime.waiting_delete_ep_from)
async def del_ep_get_from(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Raqam!")
    await state.update_data(del_ep_from=int(msg.text))
    await state.set_state(EditAnime.waiting_delete_ep_to)
    await msg.answer("Qaysi qismgacha?", reply_markup=cancel_kb)


@admin_router.message(EditAnime.waiting_delete_ep_to)
async def del_ep_get_to(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text.isdigit():
        return await msg.answer("❌ Raqam!")
    data = await state.get_data()
    from_ep = data["del_ep_from"]
    to_ep = int(msg.text)
    if to_ep < from_ep:
        return await msg.answer(f"❌ {to_ep} < {from_ep}!")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ {from_ep}-{to_ep} o'chir",
                    callback_data=f"delepconfirm_{data['del_ep_anime_id']}_{from_ep}_{to_ep}",
                    style="danger",
                ),
                InlineKeyboardButton(text="❌ Bekor", callback_data="cancel_delete", style="danger"),
            ]
        ]
    )
    await state.clear()
    await msg.answer(
        f"⚠️ <b>{data['del_ep_anime_title']}</b>\n{from_ep}-{to_ep} ({to_ep - from_ep + 1} ta) o'chirilsinmi?",
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data.startswith("delepconfirm_"))
async def confirm_del_eps(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    parts = call.data.replace("delepconfirm_", "").split("_")
    anime_id = int(parts[0])
    from_ep = int(parts[1])
    to_ep = int(parts[2])
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(Series).where(Series.anime_id == anime_id, Series.episode >= from_ep, Series.episode <= to_ep)
        )
        eps = r.scalars().all()
        for ep in eps:
            await session.delete(ep)
        await session.commit()
    await call.message.edit_text(f"✅ {from_ep}-{to_ep} ({len(eps)} ta) o'chirildi!")
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  STATISTIKA
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "📊 Statistika")
async def show_stats(msg: Message):
    if not await is_admin(msg.from_user.id):
        return
    from database.queries import get_pro_user_count, get_today_active_users

    async with AsyncSessionLocal() as session:
        u_count = await session.scalar(select(func.count(User.telegram_id)))
        pro_count = await get_pro_user_count(session)
        today_active = await get_today_active_users(session)
        a_count = await session.scalar(select(func.count(Anime.id)))
        locked_count = await session.scalar(select(func.count(Anime.id)).where(Anime.is_pro_locked == True))
        s_count = await session.scalar(select(func.count(Series.id)))
        ch_count = await session.scalar(select(func.count(SubscriptionChannel.id)))
    await msg.answer(
        f"📊 <b>Kaworai Statistika</b>\n\n"
        f"👤 Foydalanuvchilar: <b>{u_count}</b>\n"
        f"⭐ Pro: <b>{pro_count}</b>\n"
        f"🟢 Bugun faol: <b>{today_active}</b>\n\n"
        f"🎬 Kontent: <b>{a_count}</b>\n"
        f"  🔒 Pro-locked: {locked_count}\n"
        f"🎞 Qismlar: <b>{s_count}</b>\n"
        f"📢 Kanallar: <b>{ch_count}</b>",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════
#  TOP 18
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "🏆 Top 18")
async def show_top18(msg: Message):
    if not await is_admin(msg.from_user.id):
        return
    async with AsyncSessionLocal() as session:
        top_views = (await session.execute(select(Anime).order_by(Anime.views.desc()).limit(9))).scalars().all()
        top_rated = (
            (await session.execute(select(Anime).where(Anime.rating_count >= 1).order_by(Anime.rating.desc()).limit(9)))
            .scalars()
            .all()
        )

    text = "🏆 <b>TOP 18</b>\n\n👁 <b>Ko'p ko'rilgan:</b>\n"
    for i, a in enumerate(top_views, 1):
        lock = "🔒" if a.is_pro_locked else ""
        text += f"{i}. {lock}<b>{a.title}</b> — {a.views} ko'rish | ⭐{a.rating:.1f}\n"
    text += "\n⭐ <b>Yuqori reytingli:</b>\n"
    for i, a in enumerate(top_rated, 1):
        lock = "🔒" if a.is_pro_locked else ""
        text += f"{i}. {lock}<b>{a.title}</b> — ⭐{a.rating:.1f} ({a.rating_count} ovoz)\n"
    await msg.answer(text, parse_mode="HTML", reply_markup=admin_main_kb)


# ═══════════════════════════════════════════════════════════
#  KANAL SOZLAMALARI
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text.in_({"📢 Kanallar", "📢 Kanal sozlamalari"}))
async def channel_manager(msg: Message):
    if not await is_admin(msg.from_user.id):
        return
    async with AsyncSessionLocal() as session:
        channels = await get_all_channels(session)
    if not channels:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ Obuna kanali", callback_data="add_channel", style="primary"),
                    InlineKeyboardButton(text="📰 News kanal", callback_data="add_news_channel", style="primary"),
                ]
            ]
        )
        return await msg.answer(
            "📢 <b>Kanallar boshqaruvi</b>\n\n"
            "Hozircha kanallar yo'q.\n\n"
            "• <b>Obuna kanali</b> — foydalanuvchi obuna bo'lishi shart bo'lgan kanal\n"
            "• <b>News kanal</b> — yangiliklar kanali (ixtiyoriy)",
            reply_markup=kb,
            parse_mode="HTML",
        )
    text = "📢 <b>Kanallar boshqaruvi</b>\n\n"
    kb = InlineKeyboardBuilder()
    for ch in channels:
        st = "✅" if ch.is_active else "⛔"
        cht = "📰 News" if ch.is_news else ("🔒 Majburiy" if ch.require_check else "👁 Oddiy")
        text += f"{st} {cht} — <b>{ch.channel_name}</b>\n{ch.channel_url}\n\n"
        btn_t = "⛔ O'chir" if ch.is_active else "✅ Yoq"
        kb.row(
            InlineKeyboardButton(
                text=f"{btn_t} | {ch.channel_name}", callback_data=f"toggle_ch_{ch.id}", style="primary"
            ),
            InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"del_ch_{ch.id}", style="danger"),
        )
    kb.row(
        InlineKeyboardButton(text="➕ Obuna kanali", callback_data="add_channel", style="primary"),
        InlineKeyboardButton(text="📰 News kanal", callback_data="add_news_channel", style="primary"),
    )
    await msg.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("toggle_ch_"))
async def toggle_channel_cb(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    ch_id = int(call.data.replace("toggle_ch_", ""))
    async with AsyncSessionLocal() as session:
        result = await toggle_channel(session, ch_id)
    try:
        from middlewares.subscription import invalidate_active_channels_cache

        invalidate_active_channels_cache()
    except Exception:
        pass
    msg_text = "✅ Yoqildi" if result else "⛔ O'chirildi"
    await call.answer(msg_text, show_alert=True)
    await channel_manager(call.message)


@admin_router.callback_query(F.data.startswith("del_ch_"))
async def delete_channel_cb(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    ch_id = int(call.data.replace("del_ch_", ""))
    async with AsyncSessionLocal() as session:
        success = await remove_channel(session, ch_id)
    if success:
        try:
            from middlewares.subscription import invalidate_active_channels_cache

            invalidate_active_channels_cache()
        except Exception:
            pass
        await call.answer("✅ O'chirildi!", show_alert=True)
        await channel_manager(call.message)
    else:
        await call.answer("❌ Topilmadi!", show_alert=True)


@admin_router.callback_query(F.data == "add_channel")
async def start_add_channel(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AddChannel.waiting_name)
    await state.update_data(is_news_channel=False)
    await call.message.answer("1️⃣ Kanal nomini kiriting:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.callback_query(F.data == "add_news_channel")
async def add_news_channel_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AddChannel.waiting_name)
    await state.update_data(is_news_channel=True)
    await call.message.answer("📰 News kanal nomini kiriting:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.message(AddChannel.waiting_name)
async def save_ch_name(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    await state.update_data(channel_name=msg.text.strip())
    await state.set_state(AddChannel.waiting_url)
    await msg.answer("2️⃣ URL:\n<code>https://t.me/kanal</code>", parse_mode="HTML")


@admin_router.message(AddChannel.waiting_url)
async def save_ch_url(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    url = msg.text.strip()
    if not url.startswith("http"):
        return await msg.answer("❌ https:// bilan!")
    data = await state.get_data()
    is_news = data.get("is_news_channel", False)
    await state.update_data(channel_url=url)
    if is_news:
        await state.set_state(AddChannel.waiting_channel_id)
        return await msg.answer("3️⃣ Kanal ID:\n<i>-1001234567890</i>", parse_mode="HTML")
    await state.set_state(AddChannel.waiting_type)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👁 Faqat ko'rsatish", callback_data="ch_type_show", style="primary")],
            [InlineKeyboardButton(text="🔒 Majburiy obuna", callback_data="ch_type_required", style="primary")],
        ]
    )
    await msg.answer("3️⃣ Kanal turi:", reply_markup=kb)


@admin_router.callback_query(F.data == "ch_type_show")
async def ch_type_show(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    async with AsyncSessionLocal() as session:
        ch, status = await add_channel(
            session=session,
            channel_name=data["channel_name"],
            channel_url=data["channel_url"],
            require_check=False,
            is_news=False,
        )
    if status == "created":
        text = f"✅ {esc(ch.channel_name)} qo'shildi!"
    else:
        text = f"ℹ️ {esc(ch.channel_name)} allaqachon mavjud."
    await call.message.edit_text(text, parse_mode="HTML")
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data == "ch_type_required")
async def ch_type_required(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AddChannel.waiting_channel_id)
    await call.message.edit_text("4️⃣ Kanal ID:\n<i>-1001234567890</i>", parse_mode="HTML")
    await call.answer()


@admin_router.message(AddChannel.waiting_channel_id)
async def save_ch_id(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if not msg.text:
        return await msg.answer("❌ Format: <code>-1001234567890</code>", parse_mode="HTML")
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    try:
        channel_id = int(msg.text.strip())
    except ValueError:
        return await msg.answer("❌ Format: <code>-1001234567890</code>", parse_mode="HTML")
    data = await state.get_data()
    is_news = data.get("is_news_channel", False)
    owner_id = await _get_owner_ctx(msg.from_user.id)
    # News kanalda region cheklovi yo'q — darrov qo'shamiz.
    if is_news:
        await state.clear()
        return await _finalize_add_channel(
            msg=msg,
            channel_name=data["channel_name"],
            channel_url=data["channel_url"],
            channel_id=channel_id,
            is_news=True,
            region=None,
            owner_id=owner_id,
        )
    # Majburiy kanal: region scope so'raymiz.
    await state.update_data(channel_id=channel_id)
    await state.set_state(AddChannel.waiting_region_scope)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌍 Barcha viloyatlar", callback_data="chreg_all", style="primary")],
            [InlineKeyboardButton(text="📍 Faqat bitta viloyat", callback_data="chreg_specific", style="primary")],
            [InlineKeyboardButton(text="❌ Bekor", callback_data="chreg_cancel", style="danger")],
        ]
    )
    await msg.answer(
        "🌍 <b>Kanal region cheklovi:</b>\n\n"
        "• <b>Barcha viloyatlar</b> — hamma foydalanuvchilarga majburiy.\n"
        "• <b>Bitta viloyat</b> — faqat tanlangan viloyatdagi userlarga majburiy.",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def _finalize_add_channel(
    *,
    msg: Message,
    channel_name: str,
    channel_url: str,
    channel_id: int,
    is_news: bool,
    region: str | None,
    owner_id: int | None = None,
) -> None:
    """Kanalni DB'ga qo'shib, admin uchun xulosa xabarini yuboradi.

    `owner_id` hamkor (partner) uchun — u qo'shgan majburiy kanal faqat
    uning kontenti ochilganda tekshiriladi. Bosh admin qo'shgan kanal esa
    `owner_id=None` bo'lib, global middleware orqali hammaga qo'llanadi.
    """
    async with AsyncSessionLocal() as session:
        ch, status = await add_channel(
            session=session,
            channel_name=channel_name,
            channel_url=channel_url,
            require_check=not is_news,
            is_news=is_news,
            channel_id=channel_id,
            owner_id=owner_id,
        )
        # Region majburiy kanallarga DB orqali yozamiz (add_channel signaturasini
        # o'zgartirmay — oldingi imzo saqlanadi).
        if ch and not is_news:
            ch.region = region
            await session.commit()

    # Subscription middleware cache'ini tozalash — yangi kanal darrov paydo bo'lsin.
    try:
        from middlewares.subscription import invalidate_active_channels_cache

        invalidate_active_channels_cache()
    except Exception:
        logger.exception("_finalize_add_channel: failed to invalidate active channels cache")

    ch_type_label = "📰 News" if is_news else "🔒 Majburiy"
    region_line = ""
    if not is_news:
        region_line = f"\n🌍 Region: <b>{esc(region_label(region) if region else 'Hammasi')}</b>"
    name_esc = esc(ch.channel_name)
    if status == "duplicate_mandatory":
        return await msg.answer(
            f"⚠️ <b>{name_esc}</b> allaqachon 🔒 Majburiy ro'yxatda.\n"
            f"Bir xil kanalni bitta kategoriyaga ikki marta qo'shib bo'lmaydi.",
            reply_markup=admin_main_kb,
            parse_mode="HTML",
        )
    if status == "duplicate_news":
        return await msg.answer(
            f"⚠️ <b>{name_esc}</b> allaqachon 📰 News ro'yxatda.\n"
            f"Bir xil kanalni bitta kategoriyaga ikki marta qo'shib bo'lmaydi.",
            reply_markup=admin_main_kb,
            parse_mode="HTML",
        )
    if status == "merged":
        other = "📰 News" if not is_news else "🔒 Majburiy"
        return await msg.answer(
            f"✅ <b>{name_esc}</b> ikkala kategoriyada ham faol:\n"
            f"   • {ch_type_label}  (yangi qo'shildi)\n"
            f"   • {other}  (avval bor edi){region_line}\n"
            f"🆔 <code>{ch.channel_id}</code>",
            reply_markup=admin_main_kb,
            parse_mode="HTML",
        )
    await msg.answer(
        f"✅ <b>{name_esc}</b> ({ch_type_label}) qo'shildi!{region_line}\n🆔 <code>{ch.channel_id}</code>",
        reply_markup=admin_main_kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "chreg_cancel", AddChannel.waiting_region_scope)
@admin_router.callback_query(F.data == "chreg_cancel", AddChannel.waiting_region_pick)
async def ch_region_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Bekor.")
    except Exception:
        pass
    await call.message.answer("Panel:", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.callback_query(F.data == "chreg_all", AddChannel.waiting_region_scope)
async def ch_region_all(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    try:
        await call.message.edit_text("✅ Region: Barcha viloyatlar")
    except Exception:
        pass
    owner_id = await _get_owner_ctx(call.from_user.id)
    await _finalize_add_channel(
        msg=call.message,
        channel_name=data["channel_name"],
        channel_url=data["channel_url"],
        channel_id=int(data["channel_id"]),
        is_news=False,
        region=None,
        owner_id=owner_id,
    )
    await call.answer()


@admin_router.callback_query(F.data == "chreg_specific", AddChannel.waiting_region_scope)
async def ch_region_specific(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AddChannel.waiting_region_pick)
    kb = region_picker_kb(callback_prefix="chregpick_", with_cancel=True, cancel_cb="chreg_cancel")
    await call.message.edit_text("📍 Viloyatni tanlang:", reply_markup=kb)
    await call.answer()


@admin_router.callback_query(F.data.startswith("chregpick_"), AddChannel.waiting_region_pick)
async def ch_region_pick(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    code = call.data.replace("chregpick_", "", 1)
    if not is_valid_region(code):
        return await call.answer("❌ Noto'g'ri region!", show_alert=True)
    data = await state.get_data()
    await state.clear()
    try:
        await call.message.edit_text(f"✅ Region: <b>{region_label(code)}</b>", parse_mode="HTML")
    except Exception:
        pass
    owner_id = await _get_owner_ctx(call.from_user.id)
    await _finalize_add_channel(
        msg=call.message,
        channel_name=data["channel_name"],
        channel_url=data["channel_url"],
        channel_id=int(data["channel_id"]),
        is_news=False,
        region=code,
        owner_id=owner_id,
    )
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  FILTER QABUL QILISH (qismlardan keyin)
# ═══════════════════════════════════════════════════════════


@admin_router.message(AddEpisodeState.waiting_filter)
async def ep_filter_received(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    data = await state.get_data()
    anime_id = int(data["ep_anime_id"])
    title = data.get("ep_anime_title") or ""
    from database.queries import set_anime_filter

    if msg.photo:
        file_id = msg.photo[-1].file_id
        async with AsyncSessionLocal() as session:
            await set_anime_filter(session, anime_id, "photo", filter_file_id=file_id)
        await state.clear()
        await msg.answer(
            f"🎨 <b>{esc(title)}</b> uchun rasm filter saqlandi!",
            parse_mode="HTML",
            reply_markup=admin_main_kb,
        )
    elif msg.video:
        file_id = msg.video.file_id
        async with AsyncSessionLocal() as session:
            await set_anime_filter(session, anime_id, "video", filter_file_id=file_id)
        await state.clear()
        await msg.answer(
            f"🎨 <b>{esc(title)}</b> uchun video filter saqlandi!",
            parse_mode="HTML",
            reply_markup=admin_main_kb,
        )
    elif msg.text and msg.text.startswith("http"):
        async with AsyncSessionLocal() as session:
            await set_anime_filter(session, anime_id, "link", filter_url=msg.text.strip())
        await state.clear()
        await msg.answer(
            f"🎨 <b>{esc(title)}</b> uchun link filter saqlandi!",
            parse_mode="HTML",
            reply_markup=admin_main_kb,
        )
    else:
        await msg.answer("❌ Rasm, video yoki link (http bilan) yuboring.")


@admin_router.callback_query(F.data == "ep_skip_filter")
async def ep_skip_filter(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.clear()
    await call.message.answer("✅ Tayyor! Filter qo'shilmadi.", reply_markup=admin_main_kb)
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  REKLAMA BOSHQARUV
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "📣 Reklamalar")
async def ad_manager(msg: Message):
    if not await is_admin(msg.from_user.id):
        return
    from database.queries import get_all_ads

    async with AsyncSessionLocal() as session:
        ads = await get_all_ads(session)

    if not ads:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Reklama qo'shish", callback_data="add_ad", style="success")]
            ]
        )
        return await msg.answer("📣 Hozircha reklamalar yo'q.\nQo'shish uchun tugmani bosing.", reply_markup=kb)

    text = "📣 <b>Reklamalar:</b>\n\n"
    kb_builder = InlineKeyboardBuilder()
    for ad in ads:
        st = "✅" if ad.is_active else "⛔"
        url_str = " | 🔗" if ad.url else ""
        text += f"{st} #{ad.id}: {ad.text[:50]}{url_str}\n"
        btn_t = "⛔ O'chir" if ad.is_active else "✅ Yoq"
        kb_builder.row(
            InlineKeyboardButton(text=f"{btn_t} #{ad.id}", callback_data=f"toggle_ad_{ad.id}", style="primary"),
            InlineKeyboardButton(text=f"🗑 #{ad.id}", callback_data=f"del_ad_{ad.id}", style="danger"),
        )
    kb_builder.row(InlineKeyboardButton(text="➕ Reklama qo'shish", callback_data="add_ad", style="success"))
    await msg.answer(text, reply_markup=kb_builder.as_markup(), parse_mode="HTML")


@admin_router.callback_query(F.data == "add_ad")
async def add_ad_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AddAdState.waiting_text)
    await call.message.answer("📝 Reklama matnini yuboring:", reply_markup=cancel_kb)
    await call.answer()


@admin_router.message(AddAdState.waiting_text)
async def ad_text_handler(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    await state.update_data(ad_text=msg.text)
    await state.set_state(AddAdState.waiting_url)
    skip_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏭ URLsiz", callback_data="ad_skip_url", style="primary")]]
    )
    await msg.answer("🔗 Reklama URL yuboring (yoki o'tkazib yuboring):", reply_markup=skip_kb)


@admin_router.message(AddAdState.waiting_url)
async def ad_url_handler(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    data = await state.get_data()
    await state.clear()
    from database.queries import add_ad as db_add_ad

    async with AsyncSessionLocal() as session:
        ad = await db_add_ad(session, data["ad_text"], msg.text)
    from utils.ad_helpers import invalidate_ad_cache

    invalidate_ad_cache()
    await msg.answer(
        f"✅ Reklama #{ad.id} qo'shildi!\nMatn: {ad.text[:60]}\nURL: {msg.text}",
        reply_markup=admin_main_kb,
    )


@admin_router.callback_query(F.data == "ad_skip_url")
async def ad_skip_url(call: types.CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddAdState.waiting_url.state:
        return await call.answer()
    if not await is_admin(call.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    from database.queries import add_ad as db_add_ad

    async with AsyncSessionLocal() as session:
        ad = await db_add_ad(session, data["ad_text"])
    from utils.ad_helpers import invalidate_ad_cache

    invalidate_ad_cache()
    await call.message.answer(
        f"✅ Reklama #{ad.id} qo'shildi!\nMatn: {ad.text[:60]}",
        reply_markup=admin_main_kb,
    )
    await call.answer()


@admin_router.callback_query(F.data.startswith("toggle_ad_"))
async def toggle_ad_cb(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    ad_id = int(call.data.replace("toggle_ad_", ""))
    from database.queries import toggle_ad as db_toggle_ad

    async with AsyncSessionLocal() as session:
        result = await db_toggle_ad(session, ad_id)
    from utils.ad_helpers import invalidate_ad_cache

    invalidate_ad_cache()
    if result is None:
        return await call.answer("❌ Reklama topilmadi!", show_alert=True)
    st = "yoqildi ✅" if result else "o'chirildi ⛔"
    await call.answer(f"Reklama #{ad_id} {st}", show_alert=True)


@admin_router.callback_query(F.data.startswith("del_ad_"))
async def del_ad_cb(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    ad_id = int(call.data.replace("del_ad_", ""))
    from database.queries import remove_ad as db_remove_ad

    async with AsyncSessionLocal() as session:
        ok = await db_remove_ad(session, ad_id)
    from utils.ad_helpers import invalidate_ad_cache

    invalidate_ad_cache()
    if ok:
        await call.answer(f"🗑 Reklama #{ad_id} o'chirildi!", show_alert=True)
    else:
        await call.answer("❌ Topilmadi!", show_alert=True)


# ═══════════════════════════════════════════════════════════
#  REGION STATISTIKASI
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "🌍 Regionlar")
async def region_stats(msg: Message):
    if not await is_admin(msg.from_user.id):
        return
    from database.queries import get_user_count_by_region

    async with AsyncSessionLocal() as session:
        stats = await get_user_count_by_region(session)

    text = "🌍 <b>Viloyatlar bo'yicha foydalanuvchilar</b>\n\n"
    total = 0
    for region_code, count in stats:
        label = region_label(region_code)
        text += f"📍 {label}: <b>{count}</b>\n"
        total += count
    text += f"\n👥 Jami: <b>{total}</b>"
    await msg.answer(text, parse_mode="HTML", reply_markup=admin_main_kb)


# ═══════════════════════════════════════════════════════════
#  PRO YOZUVI (admin boshqaruvi)
# ═══════════════════════════════════════════════════════════


@admin_router.message(F.text == "📝 Pro yozuvi")
async def pro_text_manager(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    from database.queries import get_bot_setting

    async with AsyncSessionLocal() as session:
        current = await get_bot_setting(session, "pro_ad_text")
    display = current or "<i>Hali kiritilmagan</i>"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ O'zgartirish", callback_data="pro_text_edit", style="success")],
            [InlineKeyboardButton(text="🗑 O'chirish", callback_data="pro_text_delete", style="danger")],
        ]
    )
    await msg.answer(
        f"📝 <b>Pro yozuvi</b>\n\n"
        f"Bu yozuv Pro olishi mumkin bo'lgan foydalanuvchilarga ko'rsatiladi.\n\n"
        f"📌 Hozirgi matn:\n{display}",
        parse_mode="HTML",
        reply_markup=kb,
    )


@admin_router.callback_query(F.data == "pro_text_edit")
async def pro_text_edit_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(EditProTextState.waiting_text)
    await call.message.answer(
        "📝 Yangi Pro yozuvini kiriting (HTML format qo'llab-quvvatlanadi):",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await call.answer()


@admin_router.message(EditProTextState.waiting_text)
async def pro_text_received(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return
    if msg.text == "🚫 Bekor qilish":
        await state.clear()
        return await msg.answer("Bekor.", reply_markup=admin_main_kb)
    if not msg.text:
        return await msg.answer("❌ Matn kiriting!")
    from database.queries import set_bot_setting

    async with AsyncSessionLocal() as session:
        await set_bot_setting(session, "pro_ad_text", msg.text)
    from utils.ad_helpers import invalidate_pro_ad_cache

    invalidate_pro_ad_cache()
    await state.clear()
    await msg.answer("✅ Pro yozuvi saqlandi!", reply_markup=admin_main_kb)


@admin_router.callback_query(F.data == "pro_text_delete")
async def pro_text_delete(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    from database.queries import set_bot_setting

    async with AsyncSessionLocal() as session:
        await set_bot_setting(session, "pro_ad_text", "")
    from utils.ad_helpers import invalidate_pro_ad_cache

    invalidate_pro_ad_cache()
    await call.message.answer("🗑 Pro yozuvi o'chirildi!", reply_markup=admin_main_kb)
    await call.answer()


@admin_router.message(F.text == "🔙 Chiqish")
async def exit_admin(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return

    await state.clear()

    await msg.answer("Admin paneldan chiqildi.", reply_markup=ReplyKeyboardRemove())

    # 🔥 SHU YERGA QO‘SHASAN
    mark_admin_inactive(msg.from_user.id)
