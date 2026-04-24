"""
Kaworai Pro — Obuna to'lov tizimi
5 sahifa, bitta xabar ichida edit_message orqali navigatsiya.
"""

import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from data import config
from database.engine import AsyncSessionLocal
from database.models import Admin, User
from loader import bot
from utils.security import esc, parse_admin_ids

logger = logging.getLogger(__name__)

pro_payment_router = Router()

# ── Konfiguratsiya ─────────────────────────────────────────
# Barcha PII/to'lov sozlamalari .env dan keladi.
# Ilgari bu yerda xardkod qilingan karta raqami/egasining ismi va admin
# username bor edi — bu PII sizlanish xavfi edi. Endi default bo'sh va
# .env sozlanmasa, matnlarda "—" ko'rinadi.
PAYMENT_CHANNEL_ID = getattr(config, "PAYMENT_CHANNEL_ID", 0)
CARD_NUMBER = getattr(config, "CARD_NUMBER", "")
CARD_OWNER = getattr(config, "CARD_OWNER", "")
ADMIN_USERNAME = getattr(config, "ADMIN_USERNAME", "")
ADMIN_ID = getattr(config, "ADMIN_ID", None)

# ── Narxlar ────────────────────────────────────────────────
PLANS = {
    "1": {"label": "❤️ 1 oylik", "price": "9.000", "months": 1},
    "2": {"label": "🔥 2 oylik", "price": "16.000", "months": 2},
    "3": {"label": "❤️‍🔥 3 oylik", "price": "21.000", "months": 3},
    "6": {"label": "⚡ 6 oylik", "price": "39.000", "months": 6},
    "12": {"label": "🌙 1 yillik", "price": "69.000", "months": 12},
}


# ── FSM ────────────────────────────────────────────────────
class ProPaymentState(StatesGroup):
    waiting_receipt = State()


class AdminRejectState(StatesGroup):
    waiting_reason = State()


class AdminMsgState(StatesGroup):
    waiting_msg = State()


# ── Admin tekshirish ────────────────────────────────────────
async def _is_admin(user_id: int) -> bool:
    import os

    admins_env = parse_admin_ids(os.getenv("ADMIN_ID", ""))
    if str(user_id) in admins_env:
        return True
    if ADMIN_ID and str(user_id) == str(ADMIN_ID):
        return True
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(Admin).where(Admin.telegram_id == user_id))
        return r.scalar_one_or_none() is not None


# ── Pro tekshirish ──────────────────────────────────────────
async def _check_pro(user_id: int) -> bool:
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


# ═══════════════════════════════════════════════════════════
#  SAHIFA TEXTLARI VA KEYBOARDLARI
# ═══════════════════════════════════════════════════════════


def _page1_text() -> str:
    return (
        "⚡️ <b>Kaworai Pro obunasini sotib olish orqali siz quyidagi "
        "imkoniyatlarga ega bo'lasiz:</b>\n\n"
        "✨ Kaworai'da premium darajadagi tajriba\n"
        "🤖 Siz uchun maxsus AI tavsiyalar\n"
        "😌 Kayfiyatingizga mos kontentlar\n"
        "💎 Kam tanilgan, lekin zo'r — Hidden Gems\n"
        "📈 Tez ommalashayotgan — Trending & Rising\n"
        "▶️ Ko'rishni to'xtagan joyingizdan davom ettirish\n"
        "🔒 Faqat Pro uchun maxsus kontentlar\n"
        "🚀 Kaworai'dan maksimal zavq oling va vaqtni bekorga sarflamang\n"
        "💖 Kaworai'ni qo'llab-quvvatlang va Pro imkoniyatlarni oching\n\n"
        "👇 Agar sotib olishni istasangiz, pastdagi <b>Sotib olish</b> tugmasini bosing"
    )


def _page1_kb() -> InlineKeyboardMarkup:
    # Bot API 9.4 style ranglari: success=yashil (sotib olish), primary=ko'k (menyu).
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Sotib olish", callback_data="pro_page2", style="success")],
            [InlineKeyboardButton(text="🏠 Asosiy menyu", callback_data="main_menu", style="primary")],
        ]
    )


def _page2_text() -> str:
    return (
        "⚡️ <b>Kaworai Pro narxlari</b>\n\n"
        "Kaworai Pro bilan premium imkoniyatlarni oching:\n\n"
        "❤️ <b>1 oylik Pro:</b>\n• 9.000 so'm\n\n"
        "🔥 <b>2 oylik Pro:</b>\n• 16.000 so'm (-11% tejang)\n\n"
        "❤️‍🔥 <b>3 oylik Pro:</b>\n• 21.000 so'm (-22% tejang)\n\n"
        "⚡ <b>6 oylik Pro:</b>\n• 39.000 so'm (-28% tejang)\n\n"
        "🌙 <b>1 yillik Pro:</b>\n• 69.000 so'm (-40% tejang)\n\n"
        "💎 <i>Ko'proq ol — kamroq to'la!</i>\n"
        "🚀 Qancha uzoq muddat olsangiz, shuncha foyda siz tomonda\n\n"
        "👇 Pastdagi tugmani bosib, muddatni tanlang"
    )


def _page2_kb() -> InlineKeyboardMarkup:
    # Tarif tanlash — success (yashil), ortga — primary (ko'k).
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❤️ 1 oylik", callback_data="pro_plan_1", style="success")],
            [
                InlineKeyboardButton(text="🔥 2 oylik (-11%)", callback_data="pro_plan_2", style="success"),
                InlineKeyboardButton(text="❤️‍🔥 3 oylik (-22%)", callback_data="pro_plan_3", style="success"),
            ],
            [
                InlineKeyboardButton(text="⚡ 6 oylik (-28%)", callback_data="pro_plan_6", style="success"),
                InlineKeyboardButton(text="🌙 1 yillik (-40%)", callback_data="pro_plan_12", style="success"),
            ],
            [InlineKeyboardButton(text="🔙 Ortga", callback_data="pro_page1", style="primary")],
        ]
    )


def _page3_text(plan_key: str) -> str:
    plan = PLANS[plan_key]
    card_line = f"💳 Karta raqam: <code>{esc(CARD_NUMBER)}</code>\n" if CARD_NUMBER else ""
    owner_line = f"👤 Qabul qiluvchi: {esc(CARD_OWNER)}\n" if CARD_OWNER else ""
    admin_line = (
        f"📩 Savollar bormi?\n👉 @{esc(ADMIN_USERNAME)} ga murojaat qiling"
        if ADMIN_USERNAME
        else "📩 Savollar bormi?\n👉 Adminga murojaat qiling"
    )
    return (
        "💳 <b>Kaworai Pro obunasini faollashtirish</b>\n\n"
        "Kaworai Pro olish uchun quyidagi bosqichlarni bajaring:\n\n"
        "1️⃣ <b>To'lovni amalga oshiring</b>\n"
        f"💰 {plan['label']} narxi: <b>{plan['price']} so'm</b>\n"
        f"{card_line}{owner_line}\n"
        "2️⃣ <b>Chekni yuboring</b>\n"
        "📸 To'lov qilganingizdan so'ng, chek (skrinshot)ni "
        "pastdagi tugma orqali yuboring\n\n"
        "3️⃣ <b>Faollashtirish</b>\n"
        "✅ Adminlar tasdiqlaganidan keyin sizga Kaworai Pro avtomatik yoqiladi\n\n"
        "❗️ <b>Muhim:</b>\n"
        "• To'lovni faqat yuqoridagi karta raqamiga yuboring\n"
        "• Chek aniq va tushunarli bo'lishi kerak\n\n"
        f"{admin_line}"
    )


def _page3_kb(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📸 Chekni yuborish",
                    callback_data=f"pro_send_receipt_{plan_key}",
                    style="success",
                )
            ],
            [InlineKeyboardButton(text="🔙 Ortga", callback_data="pro_page2", style="primary")],
        ]
    )


def _page4_text() -> str:
    return (
        "📸 <b>Chekni yuborish</b>\n\n"
        "To'lovni tasdiqlash uchun <b>chek rasmini</b> yoki "
        "<b>skrinshotini</b> yuboring\n\n"
        "⚠️ <b>DIQQAT:</b>\n"
        "Soxta cheklar uchun <b>Jinoyat kodeksining 168-moddasiga</b> binoan "
        "ma'muriy va jinoiy javobgarlik belgilangan.\n\n"
        '<a href="https://lex.uz/docs/-111453">📜 Jinoyat kodeksining 168-moddasi (rasmiy)</a>\n\n'
        "👇 Chek rasmini <b>shu chatga</b> yuboring"
    )


def _page4_kb(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Ortga", callback_data=f"pro_plan_{plan_key}", style="primary")],
        ]
    )


def _admin_caption(user_id: int, username, full_name: str, plan_key: str) -> str:
    plan = PLANS[plan_key]
    # Foydalanuvchi tanlay oladigan maydonlar (full_name, username) ni
    # ishonchli emas, shuning uchun HTML injection'dan oldini olish uchun
    # ularni ekran qilamiz.
    uname = f"@{esc(username)}" if username else "—"
    return (
        "💳 <b>Yangi Pro obuna so'rovi!</b>\n\n"
        f"👤 Foydalanuvchi: <b>{esc(full_name)}</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📱 Username: {uname}\n"
        f"📦 Muddat: <b>{plan['label']}</b>\n"
        f"💰 Summa: <b>{plan['price']} so'm</b>\n\n"
        "👇 Tasdiqlash yoki rad etish:"
    )


def _admin_kb(user_id: int, plan_key: str) -> InlineKeyboardMarkup:
    # Tasdiqlash — success (yashil), rad etish — danger (qizil), xabar — primary (ko'k).
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Tasdiqlash",
                    callback_data=f"pro_confirm_{user_id}_{plan_key}",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="❌ Tasdiqlanmadi",
                    callback_data=f"pro_reject_{user_id}_{plan_key}",
                    style="danger",
                ),
            ],
            [InlineKeyboardButton(text="✉️ Xabar yuborish", callback_data=f"pro_msg_{user_id}", style="primary")],
        ]
    )


# ═══════════════════════════════════════════════════════════
#  1-SAHIFA
# ═══════════════════════════════════════════════════════════


@pro_payment_router.callback_query(F.data == "kawaii_pass")
async def pro_page1(call: CallbackQuery, state: FSMContext):
    await state.clear()

    # Pro bo'lsa — Pro menyu ko'rsatish (users_pro.py dagi handler ishlaydi)
    # Biz bu yerda faqat Pro emas userlar uchun sahifa ko'rsatamiz
    if await _check_pro(call.from_user.id):
        # Pro menyuni shu yerda ko'rsatamiz — import yo'q
        return await _show_pro_active_menu(call)

    try:
        await call.message.edit_text(
            text=_page1_text(),
            reply_markup=_page1_kb(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        await call.message.answer(
            text=_page1_text(),
            reply_markup=_page1_kb(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    await call.answer()


async def _show_pro_active_menu(call: CallbackQuery):
    """Pro bo'lgan user uchun menyu — import yo'q, to'g'ridan-to'g'ri."""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, call.from_user.id)
        until_str = ""
        if user and user.pro_until:
            until_str = f"\n📅 Pro tugash sanasi: <b>{user.pro_until.strftime('%d.%m.%Y')}</b>"

    # Pro aktiv menyu — barchasi success (yashil), chiqish primary (ko'k).
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🤖 AI Tavsiyalar", callback_data="pro_recommend", style="success"),
                InlineKeyboardButton(text="😌 Kayfiyatim", callback_data="pro_mood", style="success"),
            ],
            [
                InlineKeyboardButton(text="🔥 Trending", callback_data="pro_trending", style="success"),
                InlineKeyboardButton(text="⭐ Top reyting", callback_data="pro_top", style="success"),
            ],
            [
                InlineKeyboardButton(text="📈 Rising", callback_data="pro_rising", style="success"),
                InlineKeyboardButton(text="💎 Hidden Gems", callback_data="pro_hidden", style="success"),
            ],
            [
                InlineKeyboardButton(text="▶️ Davom ettirish", callback_data="pro_continue", style="success"),
                InlineKeyboardButton(text="👤 Mening didim", callback_data="pro_taste", style="success"),
            ],
            [InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="pro_settings", style="primary")],
            [InlineKeyboardButton(text="🏠 Asosiy menyu", callback_data="main_menu", style="primary")],
        ]
    )

    text = f"⚡ <b>Kaworai Pro</b>\n\n✅ Siz Pro foydalanuvchisiz!{until_str}\n\nNima qilmoqchisiz?"

    try:
        await call.message.edit_text(
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        await call.message.answer(text=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@pro_payment_router.callback_query(F.data == "pro_page1")
async def back_to_page1(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text(
            text=_page1_text(),
            reply_markup=_page1_kb(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  2-SAHIFA — Narxlar
# ═══════════════════════════════════════════════════════════


@pro_payment_router.callback_query(F.data == "pro_page2")
async def pro_page2(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text(
            text=_page2_text(),
            reply_markup=_page2_kb(),
            parse_mode="HTML",
        )
    except Exception:
        await call.message.answer(
            text=_page2_text(),
            reply_markup=_page2_kb(),
            parse_mode="HTML",
        )
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  3-SAHIFA — To'lov ma'lumotlari
# ═══════════════════════════════════════════════════════════


@pro_payment_router.callback_query(F.data.startswith("pro_plan_"))
async def pro_page3(call: CallbackQuery, state: FSMContext):
    plan_key = call.data.replace("pro_plan_", "")
    if plan_key not in PLANS:
        return await call.answer("❌ Noto'g'ri muddat!", show_alert=True)

    await state.update_data(plan_key=plan_key)

    try:
        await call.message.edit_text(
            text=_page3_text(plan_key),
            reply_markup=_page3_kb(plan_key),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        await call.message.answer(
            text=_page3_text(plan_key),
            reply_markup=_page3_kb(plan_key),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    await call.answer()


# ═══════════════════════════════════════════════════════════
#  4-SAHIFA — Chek yuborish ekrani
# ═══════════════════════════════════════════════════════════


@pro_payment_router.callback_query(F.data.startswith("pro_send_receipt_"))
async def pro_page4(call: CallbackQuery, state: FSMContext):
    plan_key = call.data.replace("pro_send_receipt_", "")
    await state.update_data(plan_key=plan_key)
    await state.set_state(ProPaymentState.waiting_receipt)

    try:
        await call.message.edit_text(
            text=_page4_text(),
            reply_markup=_page4_kb(plan_key),
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
    except Exception:
        await call.message.answer(
            text=_page4_text(),
            reply_markup=_page4_kb(plan_key),
            parse_mode="HTML",
        )
    await call.answer("📸 Chek rasmini yuboring")


# ═══════════════════════════════════════════════════════════
#  CHEK QABUL QILISH
# ═══════════════════════════════════════════════════════════


@pro_payment_router.message(ProPaymentState.waiting_receipt, F.photo | F.document)
async def receipt_received(msg: Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("plan_key", "1")
    plan = PLANS.get(plan_key, PLANS["1"])

    user_id = msg.from_user.id
    username = msg.from_user.username
    full_name = msg.from_user.full_name

    await state.clear()

    caption = _admin_caption(user_id, username, full_name, plan_key)
    admin_kb = _admin_kb(user_id, plan_key)

    # Admin kanaliga yuborish
    try:
        if msg.photo:
            await bot.send_photo(
                chat_id=PAYMENT_CHANNEL_ID,
                photo=msg.photo[-1].file_id,
                caption=caption,
                reply_markup=admin_kb,
                parse_mode="HTML",
            )
        else:
            await bot.send_document(
                chat_id=PAYMENT_CHANNEL_ID,
                document=msg.document.file_id,
                caption=caption,
                reply_markup=admin_kb,
                parse_mode="HTML",
            )
        channel_ok = True
    except Exception:
        channel_ok = False
        logger.exception(
            "receipt_received: failed to forward receipt to PAYMENT_CHANNEL_ID=%s user=%s plan=%s",
            PAYMENT_CHANNEL_ID,
            user_id,
            plan_key,
        )
        # Kanal ishlamasa — to'g'ridan-to'g'ri ADMIN_ID ga
        try:
            if msg.photo:
                await bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=msg.photo[-1].file_id,
                    caption=caption,
                    reply_markup=admin_kb,
                    parse_mode="HTML",
                )
            else:
                await bot.send_document(
                    chat_id=ADMIN_ID,
                    document=msg.document.file_id,
                    caption=caption,
                    reply_markup=admin_kb,
                    parse_mode="HTML",
                )
            admin_ok = True
        except Exception:
            admin_ok = False
            logger.exception(
                "receipt_received: fallback send to ADMIN_ID=%s also failed user=%s plan=%s",
                ADMIN_ID,
                user_id,
                plan_key,
            )
    else:
        admin_ok = True

    if not channel_ok and not admin_ok:
        logger.error(
            "receipt_received: receipt LOST (no admin received it) user=%s plan=%s username=%s",
            user_id,
            plan_key,
            username,
        )

    admin_line = f"\n\n📩 Savollar uchun: @{esc(ADMIN_USERNAME)}" if ADMIN_USERNAME else ""
    await msg.answer(
        "✅ <b>Chek qabul qilindi!</b>\n\n"
        f"📦 Muddat: <b>{plan['label']}</b>\n"
        f"💰 Summa: <b>{plan['price']} so'm</b>\n\n"
        "⏳ Adminlar tekshirib, tez orada Pro'ni yoqib berishadi.\n"
        "Odatda <b>5–30 daqiqa</b> ichida aktivlanadi." + admin_line,
        parse_mode="HTML",
    )


@pro_payment_router.message(ProPaymentState.waiting_receipt)
async def receipt_wrong_format(msg: Message):
    await msg.answer(
        "❌ Iltimos, <b>rasm</b> yoki <b>fayl</b> yuboring!\n\n📸 To'lov chekining skrinshoti bo'lishi kerak.",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════
#  ADMIN: TASDIQLASH
# ═══════════════════════════════════════════════════════════


@pro_payment_router.callback_query(F.data.startswith("pro_confirm_"))
async def admin_confirm_pro(call: CallbackQuery):
    if not await _is_admin(call.from_user.id):
        return await call.answer("❌ Faqat adminlar!", show_alert=True)

    parts = call.data.replace("pro_confirm_", "").split("_")
    user_id = int(parts[0])
    plan_key = parts[1]
    plan = PLANS.get(plan_key, PLANS["1"])

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return await call.answer("❌ Foydalanuvchi topilmadi!", show_alert=True)

        now = datetime.utcnow()
        if user.pro_until and user.pro_until > now:
            user.pro_until = user.pro_until + timedelta(days=30 * plan["months"])
        else:
            user.pro_until = now + timedelta(days=30 * plan["months"])

        user.is_pro = True
        await session.commit()
        until_str = user.pro_until.strftime("%d.%m.%Y")

    # Foydalanuvchiga xabar
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>Tabriklaymiz! Kaworai Pro faollashtirildi!</b>\n\n"
                f"📦 Muddat: <b>{plan['label']}</b>\n"
                f"📅 Tugash sanasi: <b>{until_str}</b>\n\n"
                "⚡ Endi barcha Pro imkoniyatlardan foydalanishingiz mumkin!\n"
                "👉 /start → <b>🟢 Kaworai Pro</b> tugmasini bosing"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(
            "admin_confirm_pro: failed to notify user=%s about Pro activation (admin=%s)",
            user_id,
            call.from_user.id,
        )

    logger.info(
        "admin_confirm_pro: admin=%s granted Pro user=%s plan=%s until=%s",
        call.from_user.id,
        user_id,
        plan_key,
        until_str,
    )

    # Admin kanalda xabarni yangilash
    try:
        old = call.message.caption or call.message.text or ""
        # Admin username / ID — HTML injection'dan himoya uchun ekran qilinadi.
        approver = f"@{esc(call.from_user.username)}" if call.from_user.username else esc(call.from_user.id)
        new = old + f"\n\n✅ <b>TASDIQLANDI</b> — {approver}"
        if call.message.caption:
            await call.message.edit_caption(caption=new, parse_mode="HTML")
        else:
            await call.message.edit_text(text=new, parse_mode="HTML")
    except Exception:
        logger.exception(
            "admin_confirm_pro: failed to update admin channel message admin=%s target=%s",
            call.from_user.id,
            user_id,
        )

    await call.answer(f"✅ Pro faollashtirildi! ({until_str})", show_alert=True)


# ═══════════════════════════════════════════════════════════
#  ADMIN: RAD ETISH
# ═══════════════════════════════════════════════════════════


@pro_payment_router.callback_query(F.data.startswith("pro_reject_"))
async def admin_reject_start(call: CallbackQuery, state: FSMContext):
    if not await _is_admin(call.from_user.id):
        return await call.answer("❌ Faqat adminlar!", show_alert=True)

    parts = call.data.replace("pro_reject_", "").split("_")
    user_id = int(parts[0])
    plan_key = parts[1]

    await state.update_data(reject_user_id=user_id, reject_plan=plan_key)
    await state.set_state(AdminRejectState.waiting_reason)

    await call.message.answer(
        "❌ <b>Rad etish sababi:</b>\n\n"
        "Sababni yozing (foydalanuvchiga yuboriladi).\n"
        "Sabab yo'q bo'lsa — <b>yo'q</b> deb yozing.",
        parse_mode="HTML",
    )
    await call.answer()


@pro_payment_router.message(AdminRejectState.waiting_reason)
async def reject_reason_received(msg: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("reject_user_id")
    await state.clear()

    reason = msg.text.strip() if msg.text and msg.text.lower() not in ("yo'q", "yoq") else None

    try:
        admin_line = f"📩 Savollar uchun: @{esc(ADMIN_USERNAME)}" if ADMIN_USERNAME else ""
        text = (
            "❌ <b>Kaworai Pro so'rovingiz rad etildi.</b>\n\n"
            # Admin kiritgan sabab — HTML teglarini ehtiyot choralari bilan
            # foydalanuvchiga yuborishdan oldin ekran qilamiz.
            + (f"📝 Sabab: {esc(reason)}\n\n" if reason else "")
            + admin_line
        )
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception:
        logger.exception(
            "reject_reason_received: failed to notify user=%s about rejection (admin=%s)",
            user_id,
            msg.from_user.id,
        )

    logger.info(
        "reject_reason_received: admin=%s rejected user=%s has_reason=%s",
        msg.from_user.id,
        user_id,
        bool(reason),
    )

    await msg.answer(f"✅ Foydalanuvchi ({user_id}) xabardor qilindi.")


# ═══════════════════════════════════════════════════════════
#  ADMIN: BOT ORQALI XABAR YUBORISH
# ═══════════════════════════════════════════════════════════


@pro_payment_router.callback_query(F.data.startswith("pro_msg_"))
async def admin_msg_start(call: CallbackQuery, state: FSMContext):
    if not await _is_admin(call.from_user.id):
        return await call.answer("❌ Faqat adminlar!", show_alert=True)

    user_id = int(call.data.replace("pro_msg_", ""))
    await state.update_data(msg_target_user=user_id)
    await state.set_state(AdminMsgState.waiting_msg)

    await call.message.answer(
        f"✉️ <b>Foydalanuvchi ({user_id}) ga xabar yozing:</b>\n\nMatn, rasm, video — barchasi bo'lishi mumkin.",
        parse_mode="HTML",
    )
    await call.answer()


@pro_payment_router.message(AdminMsgState.waiting_msg)
async def admin_msg_send(msg: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("msg_target_user")
    await state.clear()

    prefix = "✉️ <b>Admin xabari:</b>\n\n"

    try:
        if msg.photo:
            await bot.send_photo(
                chat_id=user_id,
                photo=msg.photo[-1].file_id,
                caption=prefix + (msg.caption or ""),
                parse_mode="HTML",
            )
        elif msg.video:
            await bot.send_video(
                chat_id=user_id,
                video=msg.video.file_id,
                caption=prefix + (msg.caption or ""),
                parse_mode="HTML",
            )
        elif msg.document:
            await bot.send_document(
                chat_id=user_id,
                document=msg.document.file_id,
                caption=prefix + (msg.caption or ""),
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=user_id,
                text=prefix + (msg.text or ""),
                parse_mode="HTML",
            )
        logger.info(
            "admin_msg_send: admin=%s sent message to user=%s",
            msg.from_user.id,
            user_id,
        )
        await msg.answer(f"✅ Xabar yuborildi (ID: {user_id})")
    except Exception as e:
        logger.exception(
            "admin_msg_send: failed to deliver admin message admin=%s target=%s",
            msg.from_user.id,
            user_id,
        )
        await msg.answer(f"❌ Xabar yuborib bo'lmadi: {e}")
