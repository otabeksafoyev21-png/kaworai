# Kawaii Pass menyusi uchun misol
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def get_kawaii_pass_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💎 1 oylik - 15,000 so'm", callback_data="pay_1_month"))
    builder.row(InlineKeyboardButton(text="👑 1 yillik - 120,000 so'm", callback_data="pay_1_year"))
    builder.row(InlineKeyboardButton(text="🔙 Ortga", callback_data="main_menu"))
    return builder.as_markup()
