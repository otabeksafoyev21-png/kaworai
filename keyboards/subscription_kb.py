# keyboards/subscription_kb.py

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.models import Partner


def get_subscription_keyboard(partners: list[Partner]) -> InlineKeyboardMarkup:
    """Obuna bo'lish tugmalari + tekshirish tugmasi"""
    buttons = []

    for partner in partners:
        buttons.append([InlineKeyboardButton(text=f"📢 {partner.channel_name}", url=partner.channel_url)])

    # Tekshirish tugmasi
    buttons.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_subscription")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)
