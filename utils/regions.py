"""
O'zbekiston viloyatlari (regions) ro'yxati va yordamchi funksiyalar.

Loyihada 3 joyda ishlatiladi:
  1. User /start'da viloyatni tanlaydi (handlers/users.py).
  2. Admin majburiy kanal qo'shayotganda kanalni ma'lum viloyatga biriktira
     oladi (handlers/admin.py).
  3. Admin xabar yuborayotganda faqat ma'lum viloyat foydalanuvchilariga
     yuborish tanlovi (handlers/admin.py).

Region `code` — qisqa, DB-friendly identifikator (masalan `tashkent_city`).
`label` — foydalanuvchi ko'rinishidagi yozuv. Yangi viloyat qo'shganda
shu ro'yxatga bitta tuple qo'shsangiz kifoya — hamma joyda paydo bo'ladi.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# (code, label)
REGIONS: list[tuple[str, str]] = [
    ("andijan", "Andijon"),
    ("bukhara", "Buxoro"),
    ("fergana", "Farg'ona"),
    ("jizzakh", "Jizzax"),
    ("khorezm", "Xorazm"),
    ("namangan", "Namangan"),
    ("navoi", "Navoiy"),
    ("kashkadarya", "Qashqadaryo"),
    ("karakalpakstan", "Qoraqalpog'iston"),
    ("samarkand", "Samarqand"),
    ("sirdaryo", "Sirdaryo"),
    ("surkhandarya", "Surxondaryo"),
    ("tashkent_region", "Toshkent viloyati"),
    ("tashkent_city", "Toshkent shahri"),
]

REGION_CODES: set[str] = {c for c, _ in REGIONS}
_REGION_LABELS: dict[str, str] = dict(REGIONS)


def region_label(code: str | None) -> str:
    """Region kodini foydalanuvchi ko'rinishidagi yozuvga o'giradi."""
    if not code:
        return "— (tanlanmagan)"
    return _REGION_LABELS.get(code, code)


def region_picker_kb(
    callback_prefix: str,
    *,
    with_all: bool = False,
    all_label: str = "🌍 Hammasi",
    all_code: str = "all",
    with_cancel: bool = False,
    cancel_cb: str = "region_cancel",
) -> InlineKeyboardMarkup:
    """
    Viloyat tanlash uchun 2 ustunli inline klaviatura.

    Tugma callback_data formatti: `<callback_prefix><code>`. Masalan
    `callback_prefix="setregion_"` bo'lsa tugmalar `setregion_tashkent_city`
    kabi bo'ladi. `with_all=True` bo'lsa yuqoriga "Hammasi" tugmasi qo'shiladi
    (admin uchun — barcha viloyatlar uchun kanal yoki xabar).
    """
    rows: list[list[InlineKeyboardButton]] = []
    if with_all:
        rows.append([InlineKeyboardButton(text=all_label, callback_data=f"{callback_prefix}{all_code}")])
    pair: list[InlineKeyboardButton] = []
    for code, label in REGIONS:
        pair.append(InlineKeyboardButton(text=label, callback_data=f"{callback_prefix}{code}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    if with_cancel:
        rows.append([InlineKeyboardButton(text="❌ Bekor", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_valid_region(code: str | None) -> bool:
    return bool(code) and code in REGION_CODES
