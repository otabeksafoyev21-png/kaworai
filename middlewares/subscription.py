import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update

from database.engine import AsyncSessionLocal
from database.models import User
from database.queries import get_active_channels
from utils.security import parse_admin_ids

# `parse_admin_ids` bo'sh ID'larni filtrlaydi — aks holda `""` qiymati
# admin ro'yxatida qoladi va kelajakdagi tekshiruvlarni xatolashtirishi mumkin.
ADMINS = parse_admin_ids(os.getenv("ADMIN_ID", ""))

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────
# Performance: SubscriptionMiddleware har bir xabar/callback uchun
# `get_active_channels` ni chaqiradi. 200k foydalanuvchi ko'lamida
# bu DB ga sekundiga yuzlab so'rov degani. Natija qisqa TTL xotira
# keshida saqlanadi. Admin kanal qo'shsa/o'chirsa
# `invalidate_active_channels_cache()` chaqiriladi.
# ───────────────────────────────────────────────────────────────
_ACTIVE_CHANNELS_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_ACTIVE_CHANNELS_TTL = 60.0  # soniya
_ACTIVE_CHANNELS_LOCK = asyncio.Lock()


async def _load_active_channels() -> list:
    """Baza'dan faol kanallarni oladi (keshsiz)."""
    async with AsyncSessionLocal() as session:
        return await get_active_channels(session)


async def get_cached_active_channels() -> list:
    """TTL kesh bilan faol kanallar ro'yxatini qaytaradi."""
    now = time.monotonic()
    data = _ACTIVE_CHANNELS_CACHE["data"]
    if data is not None and (now - _ACTIVE_CHANNELS_CACHE["ts"]) < _ACTIVE_CHANNELS_TTL:
        return data

    async with _ACTIVE_CHANNELS_LOCK:
        # double-check — boshqa coroutine allaqachon yangilagan bo'lishi mumkin
        now = time.monotonic()
        data = _ACTIVE_CHANNELS_CACHE["data"]
        if data is not None and (now - _ACTIVE_CHANNELS_CACHE["ts"]) < _ACTIVE_CHANNELS_TTL:
            return data
        fresh = await _load_active_channels()
        _ACTIVE_CHANNELS_CACHE["data"] = fresh
        _ACTIVE_CHANNELS_CACHE["ts"] = time.monotonic()
        return fresh


def invalidate_active_channels_cache() -> None:
    """Admin kanal qo'shgani/o'chirganida/yoqqanida chaqiriladi."""
    _ACTIVE_CHANNELS_CACHE["data"] = None
    _ACTIVE_CHANNELS_CACHE["ts"] = 0.0


def get_sub_keyboard(channels: list):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(text=f"📢 {ch.channel_name}", url=ch.channel_url)])
    buttons.append(
        [
            InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_subs"),
            InlineKeyboardButton(text="❌ Chiqish", callback_data="cancel_sub_check"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="⚡ Pro olish — kanalsiz kirish",
                callback_data="kawaii_pass",
                style="success",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _check_one(bot, user_id: int, ch) -> Any:
    """Bitta kanalga obuna tekshiruvi — timeout bilan himoyalangan."""
    if not ch.require_check or not ch.channel_id:
        return None
    try:
        member = await asyncio.wait_for(
            bot.get_chat_member(chat_id=ch.channel_id, user_id=user_id),
            timeout=5.0,
        )
        if member.status in ("left", "kicked", "banned"):
            return ch
    except asyncio.TimeoutError:
        # Sekin/ishlamayotgan kanal — foydalanuvchini bloklamaymiz.
        logger.warning(
            "subscription: get_chat_member timed out channel=%s user=%s",
            getattr(ch, "channel_id", None),
            user_id,
        )
    except Exception:
        # Kanal o'chirilgan, bot admin emas va hokazo — bloklamaymiz.
        logger.debug(
            "subscription: get_chat_member failed channel=%s user=%s",
            getattr(ch, "channel_id", None),
            user_id,
            exc_info=True,
        )
    return None


def _channel_matches_region(ch, user_region: str | None) -> bool:
    """Region cheklovli kanal foydalanuvchining viloyatiga mos keladimi?"""
    ch_region = getattr(ch, "region", None)
    if not ch_region:
        # Region belgilanmagan kanal — hamma uchun.
        return True
    return ch_region == user_region


def _required_channels_for(channels: list, user_region: str | None) -> list:
    """Foydalanuvchiga nisbatan majburiy kanallar (region hisobga olingan)."""
    return [ch for ch in channels if ch.require_check and ch.channel_id and _channel_matches_region(ch, user_region)]


async def check_subscription(bot, user_id: int, channels: list, user_region: str | None = None) -> list:
    """
    Faqat require_check=True va channel_id mavjud kanallarni tekshiradi.
    Qolganlar — faqat ko'rsatiladi, tekshirilmaydi.

    Region cheklovi: `ch.region` bo'sh bo'lsa kanal hammaga tegishli. Aks
    holda faqat `user_region` mos tushsa tekshiriladi. `user_region=None`
    bo'lsa (masalan, /start'dan keyingi eski xabar) — faqat region'siz
    umumiy majburiy kanallar tekshiriladi.

    Tekshiruv parallel ravishda bajariladi — ketma-ket loop har kanal
    uchun Telegram API kechikishini user-kutish vaqtiga qo'shib yuboradi.
    """
    relevant = _required_channels_for(channels, user_region)
    if not relevant:
        return []
    results = await asyncio.gather(
        *(_check_one(bot, user_id, ch) for ch in relevant),
        return_exceptions=False,
    )
    return [ch for ch in results if ch is not None]


async def _get_user_region(user_id: int) -> str | None:
    """User.region ni DB'dan oladi. Xato bo'lsa None qaytaradi."""
    try:
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(User.region).where(User.telegram_id == user_id))
            return res.scalar_one_or_none()
    except Exception:
        logger.exception("subscription: failed to fetch user region uid=%s", user_id)
        return None


def _optional_channels(channels: list) -> list:
    """require_check=False bo'lgan aktiv kanallar — faqat ko'rsatiladi."""
    return [ch for ch in channels if not ch.require_check]


def _build_sub_payload(required_not_subbed: list, optional_channels: list) -> tuple[str, Any]:
    """
    Foydalanuvchi uchun "Obuna bo'ling" ekranining matni va klaviaturasi.
    Majburiylar alohida, ixtiyoriylar pastda ko'rsatiladi (lekin
    tekshirilmaydi).
    """
    lines: list[str] = [
        "⚠️ <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:</b>",
        "<i>💡 Yoki ⚡ Kaworai Pro sotib olib, kanalsiz kiring!</i>",
        "",
    ]
    lines.extend(f"• {ch.channel_name}" for ch in required_not_subbed)
    if optional_channels:
        lines.append("")
        lines.append("🔹 <i>Ixtiyoriy kanallar (obuna shart emas):</i>")
        lines.extend(f"• {ch.channel_name}" for ch in optional_channels)
    # Klaviaturada required + optional hammasi ko'rinadi, lekin "Tekshirish"
    # faqat majburiylar bo'yicha ishlaydi (check_subscription region-aware).
    kb_channels = list(required_not_subbed) + list(optional_channels)
    return "\n".join(lines), get_sub_keyboard(kb_channels)


async def _is_pro_user(user_id: int) -> bool:
    """Pro foydalanuvchini tekshiradi — agar faol Pro bo'lsa True.

    Majburiy kanal tekshiruvi Pro foydalanuvchilar uchun o'tkazib yuboriladi
    (ular kontentni yuklab olish/ulashish imtiyozlariga ega). DB xatosida
    False qaytaradi — xavfsiz default.
    """
    try:
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(User.is_pro, User.pro_until).where(User.telegram_id == user_id))
            row = res.one_or_none()
            if not row:
                return False
            is_pro, pro_until = row
            if not is_pro:
                return False
            if pro_until and pro_until < datetime.utcnow():
                return False
            return True
    except Exception:
        logger.debug("is_pro_user: check failed uid=%s", user_id, exc_info=True)
        return False


async def _touch_last_active(user_id: int) -> None:
    """Foydalanuvchi `last_active` vaqtini yangilaydi va eslatma bosqichini
    nolga tushuradi.

    Re-engagement scheduler shu maydonni kuzatadi — agar user bot bilan
    aloqaga chiqsa, eslatma yuborilmaydi. Xato bo'lsa — jim. DB uzilganda
    bot javob berishda davom etadi.
    """
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(User)
                .where(User.telegram_id == user_id)
                .values(last_active=func.now(), last_reminder_at=None, reminder_stage=0)
            )
            await session.commit()
    except Exception:
        logger.debug("touch_last_active: update failed uid=%s", user_id, exc_info=True)


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[Any, dict], Awaitable[Any]], event: Any, data: dict) -> Any:
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        else:
            return await handler(event, data)

        # Re-engagement uchun `last_active` ni yangilaymiz — admin yoki
        # oddiy foydalanuvchi farqi yo'q. DB yozuvi hali bo'lmasa
        # (masalan /start'dan oldingi callback) UPDATE samarasiz — muammo yo'q.
        if user and user.id:
            await _touch_last_active(user.id)

        # Admin — to'siqsiz
        if str(user.id) in ADMINS:
            return await handler(event, data)

        # Pro foydalanuvchi — majburiy kanal tekshiruvidan ozod.
        # Pro imtiyozlari: kontentni yuklash/ulashish + kanalsiz kirish.
        if await _is_pro_user(user.id):
            return await handler(event, data)

        # Bu callbacklar — to'siqsiz
        if isinstance(event, CallbackQuery) and event.data in ("check_subs", "cancel_sub_check"):
            return await handler(event, data)
        # User region tanlash callbacklari — to'siqsiz (region tanlanmay
        # turib tekshirsak, user hech qachon region'ga tegishli kanalga
        # obuna bo'la olmaydi).
        if isinstance(event, CallbackQuery) and event.data and event.data.startswith("userregion_"):
            return await handler(event, data)

        try:
            channels = await get_cached_active_channels()
        except Exception:
            # DB vaqtinchalik yiqilgan bo'lsa — foydalanuvchini bloklamaslik
            # yaxshiroq, chunki bu UX uchun juda ko'rinadigan muammo.
            logger.exception("subscription: failed to load active channels")
            return await handler(event, data)

        if not channels:
            return await handler(event, data)

        user_region = await _get_user_region(user.id)
        bot = data.get("bot") or event.bot
        not_subbed = await check_subscription(bot, user.id, channels, user_region)

        if not_subbed:
            text, kb = _build_sub_payload(not_subbed, _optional_channels(channels))
            if isinstance(event, Message):
                await event.answer(text, reply_markup=kb, parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                try:
                    await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
                await event.answer()
            return

        return await handler(event, data)
