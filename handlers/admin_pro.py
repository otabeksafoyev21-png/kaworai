"""
Admin panel qo'shimcha buyruqlari:
  /add_admin <user_id>     — yangi admin qo'shish
  /remove_admin <user_id>  — adminni o'chirish
  /admins                  — adminlar ro'yxati
  /set_pro <id> [kun]      — pro berish
  /remove_pro <id>         — pro olish
  /user_info <id>          — user ma'lumoti
  /pro_users               — pro userlar
  /anime_info <id>         — anime to'liq ma'lumot
  /pro_stats               — statistika
"""

import logging
import os
from datetime import datetime, timedelta

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from database.engine import AsyncSessionLocal
from database.models import Admin, Anime, AnimeSubscription, Series, User
from database.queries import get_anime_full_info
from utils.security import esc, parse_admin_ids

logger = logging.getLogger(__name__)

pro_admin_router = Router()

# Bo'sh ID'larni filtrlash — `"".split(",")` `[""]` qaytaradi, natijada
# har qanday foydalanuvchi bo'sh string bilan taqqoslansa admin deb
# tan olinishi xavfi bor.
OWNER_IDS = parse_admin_ids(os.getenv("ADMIN_ID", ""))  # .env dagi asosiy adminlar


def _is_owner(user_id: int) -> bool:
    """Asosiy owner — faqat .env dagi ADMIN_ID lar."""
    return str(user_id) in OWNER_IDS


async def _is_admin(user_id: int) -> bool:
    """Owner yoki DB dagi admin."""
    if _is_owner(user_id):
        return True
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(Admin).where(Admin.telegram_id == user_id))
        return r.scalar_one_or_none() is not None


def _yes_no(val: bool) -> str:
    return "Ha" if val else "Yo'q"


# ═══════════════════════════════════════════════════════════
#  ADMIN QO'SHISH / O'CHIRISH
# ═══════════════════════════════════════════════════════════


@pro_admin_router.message(Command("add_admin"))
async def add_admin_cmd(msg: Message):
    if not _is_owner(msg.from_user.id):
        return await msg.answer("❌ Faqat asosiy owner admin qo'sha oladi!")

    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer(
            "Format: <code>/add_admin 123456789</code>\nIxtiyoriy: <code>/add_admin 123456789 Ism</code>",
            parse_mode="HTML",
        )

    new_admin_id = int(parts[1])

    # Ownerning o'zini qo'shishni oldini olish (redundant lekin xavfsiz)
    if _is_owner(new_admin_id) and new_admin_id != msg.from_user.id:
        return await msg.answer("⚠️ Bu foydalanuvchi allaqachon owner!")

    nickname = " ".join(parts[2:]) if len(parts) > 2 else None

    async with AsyncSessionLocal() as session:
        existing = await session.get(Admin, new_admin_id)
        if existing:
            return await msg.answer(f"⚠️ <code>{new_admin_id}</code> allaqachon admin!", parse_mode="HTML")

        new_admin = Admin(
            telegram_id=new_admin_id,
            nickname=nickname,
            role="admin",
            added_by=msg.from_user.id,
        )
        session.add(new_admin)
        await session.commit()

    # Yangi adminga xabar
    try:
        await msg.bot.send_message(
            chat_id=new_admin_id,
            text=("✅ <b>Siz Kaworai botiga admin qilib qo'shildingiz!</b>\n\nAdmin panelga kirish: /admin"),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(
            "add_admin_cmd: failed to notify new admin=%s (owner=%s)",
            new_admin_id,
            msg.from_user.id,
        )

    logger.info(
        "add_admin_cmd: owner=%s added admin=%s nickname=%s",
        msg.from_user.id,
        new_admin_id,
        nickname,
    )

    nick_str = f" ({nickname})" if nickname else ""
    await msg.answer(f"✅ <code>{new_admin_id}</code>{nick_str} admin qilindi!", parse_mode="HTML")


@pro_admin_router.message(Command("remove_admin"))
async def remove_admin_cmd(msg: Message):
    if not _is_owner(msg.from_user.id):
        return await msg.answer("❌ Faqat asosiy owner adminni o'chira oladi!")

    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer("Format: <code>/remove_admin 123456789</code>", parse_mode="HTML")

    target_id = int(parts[1])

    # Ownerning o'zini o'chirishga urinish
    if _is_owner(target_id):
        return await msg.answer("❌ Owner adminni o'chirib bo'lmaydi!")

    async with AsyncSessionLocal() as session:
        admin = await session.get(Admin, target_id)
        if not admin:
            return await msg.answer(f"❌ <code>{target_id}</code> admin emas!", parse_mode="HTML")

        await session.delete(admin)
        await session.commit()

    try:
        await msg.bot.send_message(
            chat_id=target_id, text="❌ <b>Admin huquqingiz olib tashlandi.</b>", parse_mode="HTML"
        )
    except Exception:
        logger.exception(
            "remove_admin_cmd: failed to notify removed admin=%s (owner=%s)",
            target_id,
            msg.from_user.id,
        )

    logger.info(
        "remove_admin_cmd: owner=%s removed admin=%s",
        msg.from_user.id,
        target_id,
    )

    await msg.answer(f"✅ <code>{target_id}</code> admin huquqi olib tashlandi.", parse_mode="HTML")


@pro_admin_router.message(Command("admins"))
async def list_admins_cmd(msg: Message):
    if not await _is_admin(msg.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Admin).order_by(Admin.added_at.desc()))
        admins = result.scalars().all()

    owner_list = ", ".join(f"<code>{oid}</code>" for oid in OWNER_IDS)
    text = f"👑 <b>Ownerlar:</b> {owner_list}\n\n"

    if admins:
        text += f"🛠 <b>Adminlar ({len(admins)} ta):</b>\n\n"
        for i, a in enumerate(admins, 1):
            nick = esc(a.nickname) if a.nickname else "—"
            added_at = a.added_at.strftime("%d.%m.%Y") if a.added_at else "—"
            text += f"{i}. <code>{a.telegram_id}</code> — {nick}\n   Rol: {esc(a.role)} | Qo'shilgan: {added_at}\n\n"
    else:
        text += "🛠 Qo'shimcha adminlar yo'q."

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="admin_add_hint", style="success")]
        ]
    )
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@pro_admin_router.callback_query(F.data == "admin_add_hint")
async def admin_add_hint(call: types.CallbackQuery):
    await call.answer("Admin qo'shish:\n/add_admin 123456789", show_alert=True)


# ═══════════════════════════════════════════════════════════
#  PRO USER BOSHQARUV — ID ORQALI
# ═══════════════════════════════════════════════════════════


@pro_admin_router.message(Command("set_pro"))
async def set_pro_cmd(msg: Message):
    if not await _is_admin(msg.from_user.id):
        return

    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer(
            "Format:\n"
            "<code>/set_pro 123456789</code>      — 30 kun\n"
            "<code>/set_pro 123456789 60</code>   — 60 kun\n"
            "<code>/set_pro 123456789 0</code>    — abadiy",
            parse_mode="HTML",
        )

    user_id = int(parts[1])
    days = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 30

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await msg.answer(f"❌ User <code>{user_id}</code> topilmadi!", parse_mode="HTML")

        now = datetime.utcnow()
        if days == 0:
            user.pro_until = None
            until_str = "Abadiy ♾️"
        else:
            base = user.pro_until if (user.pro_until and user.pro_until > now) else now
            user.pro_until = base + timedelta(days=days)
            until_str = user.pro_until.strftime("%d.%m.%Y")

        user.is_pro = True
        await session.commit()
        full_name = user.full_name or str(user_id)

    try:
        await msg.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>Kaworai Pro faollashtirildi!</b>\n\n"
                f"📅 Tugash sanasi: <b>{until_str}</b>\n\n"
                "⚡ Barcha Pro imkoniyatlardan foydalaning!\n"
                "👉 /start → 🟢 Kaworai Pro"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(
            "set_pro_cmd: failed to notify user=%s about Pro activation (admin=%s)",
            user_id,
            msg.from_user.id,
        )

    logger.info(
        "set_pro_cmd: admin=%s granted Pro user=%s days=%s until=%s",
        msg.from_user.id,
        user_id,
        days,
        until_str,
    )

    await msg.answer(
        f"✅ <b>{esc(full_name)}</b> (<code>{user_id}</code>) Pro qilindi!\n📅 Muddat: {until_str}", parse_mode="HTML"
    )


@pro_admin_router.message(Command("remove_pro"))
async def remove_pro_cmd(msg: Message):
    if not await _is_admin(msg.from_user.id):
        return

    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer("Format: <code>/remove_pro 123456789</code>", parse_mode="HTML")

    user_id = int(parts[1])
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await msg.answer(f"❌ User <code>{user_id}</code> topilmadi!", parse_mode="HTML")

        user.is_pro = False
        user.pro_until = None
        await session.commit()
        full_name = user.full_name or str(user_id)

    try:
        await msg.bot.send_message(
            chat_id=user_id, text="❌ <b>Kaworai Pro obunangiz bekor qilindi.</b>", parse_mode="HTML"
        )
    except Exception:
        logger.exception(
            "remove_pro_cmd: failed to notify user=%s (admin=%s)",
            user_id,
            msg.from_user.id,
        )

    logger.info(
        "remove_pro_cmd: admin=%s removed Pro user=%s",
        msg.from_user.id,
        user_id,
    )

    await msg.answer(f"✅ <b>{esc(full_name)}</b> (<code>{user_id}</code>) Pro olib tashlandi.", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  USER INFO
# ═══════════════════════════════════════════════════════════


@pro_admin_router.message(Command("user_info"))
async def user_info_cmd(msg: Message):
    if not await _is_admin(msg.from_user.id):
        return

    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer("Format: <code>/user_info 123456789</code>", parse_mode="HTML")

    user_id = int(parts[1])
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await msg.answer(f"❌ User <code>{user_id}</code> topilmadi!", parse_mode="HTML")

        try:
            from database.models import UserWatchHistory

            watch_count = (
                await session.execute(
                    select(func.count(UserWatchHistory.id)).where(UserWatchHistory.user_id == user_id)
                )
            ).scalar() or 0
        except Exception:
            logger.exception("user_info_cmd: watch count failed user=%s", user_id)
            watch_count = 0

        try:
            sub_count = (
                await session.execute(
                    select(func.count(AnimeSubscription.user_id)).where(AnimeSubscription.user_id == user_id)
                )
            ).scalar() or 0
        except Exception:
            logger.exception("user_info_cmd: subscription count failed user=%s", user_id)
            sub_count = 0

    now = datetime.utcnow()
    is_pro = user.is_pro and (not user.pro_until or user.pro_until > now)

    if user.pro_until:
        days_left = (user.pro_until - now).days
        until_full = f"{user.pro_until.strftime('%d.%m.%Y')} ({days_left} kun qoldi)"
    else:
        until_full = "Abadiy ♾️" if user.is_pro else "—"

    pro_status = "✅ Ha" if is_pro else "❌ Yo'q"
    joined_str = user.joined_at.strftime("%d.%m.%Y") if user.joined_at else "—"

    # Foydalanuvchining full_name va username ishonchli emas —
    # HTML injection'dan himoyalanish uchun ekran qilamiz.
    fn_display = esc(user.full_name) if user.full_name else "—"
    un_display = esc(user.username) if user.username else "—"
    text = (
        f"👤 <b>Foydalanuvchi</b>\n\n"
        f"🆔 ID: <code>{user.telegram_id}</code>\n"
        f"📛 Ism: <b>{fn_display}</b>\n"
        f"🔗 @{un_display}\n"
        f"📅 Ro'yxatdan: {joined_str}\n\n"
        f"⭐ Pro: {pro_status}\n"
        f"📅 Pro tugashi: {until_full}\n\n"
        f"🎬 Ko'rgan: {watch_count} ta\n"
        f"🔔 Obunalar: {sub_count} ta"
    )

    # Pro berish — success (yashil), olib tashlash/qisqartirish — danger (qizil), xabar — primary.
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ 30 kun Pro", callback_data=f"usr_pro_{user_id}_30", style="success"),
                InlineKeyboardButton(text="✅ 90 kun Pro", callback_data=f"usr_pro_{user_id}_90", style="success"),
            ],
            [
                InlineKeyboardButton(text="❌ Pro olish", callback_data=f"usr_remvpro_{user_id}", style="danger"),
            ],
            [
                InlineKeyboardButton(text="📉 7 kun qisq.", callback_data=f"usr_reduce_{user_id}_7", style="danger"),
                InlineKeyboardButton(
                    text="📉 30 kun qisq.",
                    callback_data=f"usr_reduce_{user_id}_30",
                    style="danger",
                ),
            ],
            [
                InlineKeyboardButton(text="✉️ Xabar yuborish", callback_data=f"pro_msg_{user_id}", style="primary"),
            ],
        ]
    )
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@pro_admin_router.callback_query(F.data.startswith("usr_pro_"))
async def usr_pro_give(call: types.CallbackQuery):
    if not await _is_admin(call.from_user.id):
        return

    parts = call.data.replace("usr_pro_", "").split("_")
    user_id = int(parts[0])
    days = int(parts[1]) if len(parts) > 1 else 30

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await call.answer("❌ User topilmadi!", show_alert=True)

        now = datetime.utcnow()
        base = user.pro_until if (user.pro_until and user.pro_until > now) else now
        user.pro_until = base + timedelta(days=days)
        user.is_pro = True
        await session.commit()
        until_str = user.pro_until.strftime("%d.%m.%Y")

    try:
        await call.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>Kaworai Pro faollashtirildi!</b>\n\n"
                f"📅 Tugash sanasi: <b>{until_str}</b>\n\n"
                "👉 /start → 🟢 Kaworai Pro"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(
            "usr_pro_give: failed to notify user=%s (admin=%s)",
            user_id,
            call.from_user.id,
        )

    logger.info(
        "usr_pro_give: admin=%s granted Pro user=%s days=%s until=%s",
        call.from_user.id,
        user_id,
        days,
        until_str,
    )

    await call.answer(f"✅ {days} kunlik Pro berildi! ({until_str})", show_alert=True)


@pro_admin_router.callback_query(F.data.startswith("usr_remvpro_"))
async def usr_remove_pro(call: types.CallbackQuery):
    if not await _is_admin(call.from_user.id):
        return

    user_id = int(call.data.replace("usr_remvpro_", ""))
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await call.answer("❌ User topilmadi!", show_alert=True)
        user.is_pro = False
        user.pro_until = None
        await session.commit()

    try:
        await call.bot.send_message(
            chat_id=user_id, text="❌ <b>Kaworai Pro obunangiz bekor qilindi.</b>", parse_mode="HTML"
        )
    except Exception:
        logger.exception(
            "usr_remove_pro: failed to notify user=%s (admin=%s)",
            user_id,
            call.from_user.id,
        )

    logger.info(
        "usr_remove_pro: admin=%s removed Pro user=%s",
        call.from_user.id,
        user_id,
    )

    await call.answer("✅ Pro olib tashlandi!", show_alert=True)


@pro_admin_router.callback_query(F.data.startswith("usr_reduce_"))
async def usr_reduce_pro(call: types.CallbackQuery):
    if not await _is_admin(call.from_user.id):
        return

    parts = call.data.replace("usr_reduce_", "").split("_")
    user_id = int(parts[0])
    days = int(parts[1])

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
                result_msg = f"Pro {days} kun qisqartirildi — muddati tugadi."
            else:
                result_msg = f"Pro {days} kun qisqartirildi. Yangi: {user.pro_until.strftime('%d.%m.%Y')}"
        else:
            result_msg = "Abadiy Pro ni qisqartirish mumkin emas!"
        await session.commit()

    await call.answer(result_msg, show_alert=True)


# ═══════════════════════════════════════════════════════════
#  ANIME INFO
# ═══════════════════════════════════════════════════════════


@pro_admin_router.message(Command("anime_info"))
async def anime_info_cmd(msg: Message):
    if not await _is_admin(msg.from_user.id):
        return

    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await msg.answer("Format: <code>/anime_info 1234</code>", parse_mode="HTML")

    anime_id = int(parts[1])
    async with AsyncSessionLocal() as session:
        info = await get_anime_full_info(session, anime_id)

    if not info:
        return await msg.answer(f"❌ ID {anime_id} topilmadi!")

    genres_str = ", ".join(info["genres"][:5]) or "—"
    tags_str = ", ".join(info["tags"][:5]) or "—"
    mood_str = ", ".join(info["mood"][:3]) or "—"
    added_at = info["added_at"].strftime("%d.%m.%Y %H:%M") if info.get("added_at") else "Nomalum"
    added_by = "Nomalum"
    if info.get("added_by_id"):
        added_by = f"<code>{info['added_by_id']}</code>"
        if info.get("added_by_username"):
            added_by += f" (@{info['added_by_username']})"

    type_emoji = {"anime": "🎌", "movie": "🎥", "serial": "📺", "dorama": "🌸"}
    emoji = type_emoji.get(info["type"], "🎬")

    # Anime metadata adminlar kiritadi, lekin ba'zi maydonlar tashqi
    # manbalardan olingan bo'lishi mumkin — HTML xavfsiz bo'lishi uchun
    # barcha dinamik satrlarni ekran qilamiz.
    text = (
        f"📋 <b>Anime ma'lumotlari</b>\n\n"
        f"{emoji} <b>{esc(info['title'])}</b>" + (f" ({info['year']})" if info.get("year") else "") + "\n"
        f"🆔 ID: <code>{info['id']}</code>\n"
        f"📁 Tur: {info['type']}\n"
        f"📊 Status: {info.get('status', '—')}\n\n"
        f"🎭 Janr: {genres_str}\n"
        f"🏷 Teglar: {tags_str}\n"
        f"😌 Mood: {mood_str}\n\n"
        f"⭐ Reyting: <b>{info['rating']:.1f}</b> ({info['rating_count']} ovoz)\n"
        f"👁 Ko'rishlar: <b>{info['views']}</b>\n"
        f"🎞 Qismlar: <b>{info['episodes_count']}</b>\n\n"
        f"🔔 Obunalar: <b>{info['subscribers']}</b> ta\n"
        f"  ⭐ Pro obunalar: <b>{info['pro_subscribers']}</b> ta\n\n"
        f"🔒 Pro-locked: {_yes_no(info['is_pro_locked'])}\n"
        f"💎 Hidden Gem: {_yes_no(info['is_hidden_gem'])}\n\n"
        f"👤 Qo'shgan: {added_by}\n"
        f"📅 Qo'shilgan: {added_at}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Pro-lock toggle",
                    callback_data=f"adm_prolock_{anime_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📢 Kanalga post",
                    callback_data=f"postch_all_{anime_id}",
                    style="success",
                ),
            ]
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
        logger.exception(
            "anime_info_cmd: failed to render poster for anime=%s, falling back to text",
            anime_id,
        )
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@pro_admin_router.callback_query(F.data.startswith("adm_prolock_"))
async def adm_prolock_toggle(call: types.CallbackQuery):
    if not await _is_admin(call.from_user.id):
        return
    anime_id = int(call.data.replace("adm_prolock_", ""))
    async with AsyncSessionLocal() as session:
        anime = await session.get(Anime, anime_id)
        if anime:
            anime.is_pro_locked = not anime.is_pro_locked
            await session.commit()
            status = "🔒 Pro-locked" if anime.is_pro_locked else "🔓 Ochiq"
            await call.answer(f"✅ {anime.title}: {status}", show_alert=True)


# ═══════════════════════════════════════════════════════════
#  PRO USERLAR RO'YXATI
# ═══════════════════════════════════════════════════════════


@pro_admin_router.message(Command("pro_users"))
async def list_pro_users(msg: Message):
    if not await _is_admin(msg.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.is_pro == True).order_by(User.pro_until.desc()))
        users = result.scalars().all()

    if not users:
        return await msg.answer("❌ Pro foydalanuvchilar yo'q!")

    now = datetime.utcnow()
    text = f"⭐ <b>Pro foydalanuvchilar ({len(users)} ta):</b>\n\n"

    for i, u in enumerate(users[:30], 1):
        uname = f"@{esc(u.username)}" if u.username else "—"
        if u.pro_until:
            days_left = (u.pro_until - now).days
            until_str = f"{u.pro_until.strftime('%d.%m.%Y')} ({days_left}k)"
        else:
            until_str = "Abadiy"
        expired = " ⚠️" if (u.pro_until and u.pro_until < now) else ""
        text += f"{i}. <code>{u.telegram_id}</code> {uname}\n   📅 {until_str}{expired}\n\n"

    await msg.answer(text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  PRO STATISTIKA
# ═══════════════════════════════════════════════════════════


@pro_admin_router.message(Command("pro_stats"))
async def pro_stats_cmd(msg: Message):
    if not await _is_admin(msg.from_user.id):
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

    top3_text = "\n".join(f"  {i + 1}. {esc(r[0])} — {r[1]} ko'rish" for i, r in enumerate(top3))

    await msg.answer(
        f"📊 <b>Kaworai Pro Statistika</b>\n\n"
        f"👤 Jami: <b>{total_users}</b>\n"
        f"⭐ Pro: <b>{pro_users}</b>\n"
        f"  ⚠️ Muddati o'tgan: {expired}\n\n"
        f"🎬 Kontent: <b>{total_animes}</b>\n"
        f"  🔒 Pro-locked: {locked_count}\n"
        f"🎞 Qismlar: <b>{ep_count}</b>\n\n"
        f"🔥 Top 3:\n{top3_text}",
        parse_mode="HTML",
    )
