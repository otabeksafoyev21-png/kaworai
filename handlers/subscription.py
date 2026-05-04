# handlers/subscription.py

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import async_sessionmaker

from database.queries.partners import get_active_partners
from keyboards.subscription_kb import get_subscription_keyboard
from loader import bot

subscription_router = Router()


@subscription_router.callback_query(lambda c: c.data == "check_subscription")
async def check_subscription_handler(call: CallbackQuery, session_maker: async_sessionmaker):
    """Foydalanuvchi 'Tekshirish' tugmasini bosdi"""
    async with session_maker() as session:
        partners = await get_active_partners(session)

    not_subscribed = []
    for partner in partners:
        try:
            member = await bot.get_chat_member(chat_id=partner.channel_id, user_id=call.from_user.id)
            if member.status in ("left", "kicked", "banned"):
                not_subscribed.append(partner)
        except Exception:
            continue

    if not_subscribed:
        # Hali ham obuna bo'lmagan kanallar bor
        text = (
            "❌ <b>Siz hali barcha kanallarga obuna bo'lmagansiz!</b>\n\n"
            "Quyidagi kanallarga obuna bo'lib, qayta tekshiring:"
        )
        kb = get_subscription_keyboard(not_subscribed)
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer("❌ Hali obuna bo'lmagansiz!", show_alert=True)
    else:
        # Hammaga obuna bo'ldi!
        await call.message.delete()
        await call.message.answer(
            "✅ <b>Rahmat! Endi botdan to'liq foydalanishingiz mumkin.</b>\n\n🎌 /start — boshiga qaytish"
        )
        await call.answer("✅ Obuna tasdiqlandi!", show_alert=True)
