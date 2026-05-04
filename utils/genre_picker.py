"""
Janr multi-select picker — admin anime qo'shish va tahrirlash jarayoni uchun.

Qoida: `handlers/genres.py`'dagi `GENRES` dictining kalitlari — janr kanonik
nomi (o'zbekcha, masalan `"Jang"`). Foydalanuvchi janr tugmasini bossa,
saqlanadigan qiymat ham shu kalit bo'ladi. Bu orqali "janr bo'yicha qidirish"
aniq mos keladi — admin kiritgan yozma matn bilan farq qilmaydi.

Picker ikkita joyda ishlatiladi:
  * Yangi anime qo'shish (AddAnime.waiting_genres)
  * Mavjud animeni tahrirlash (EditAnime.picking_genres)

Callback prefiksi ikki tomon uchun har xil — shu orqali handlerlar
aralashib ketmaydi.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.genres import GENRES


def genre_picker_kb(selected: list[str], prefix: str) -> InlineKeyboardMarkup:
    """
    Multi-select janr klaviaturasi.

    :param selected: hozircha tanlangan janr kalitlari ro'yxati (GENRES keys).
    :param prefix:   callback prefiksi; "ag" — add anime, "eg" — edit anime.

    Callbacklar:
      - {prefix}_tog:<key>  — bitta janrni toggle qiladi
      - {prefix}_done       — tanlovni tasdiqlab keyingi bosqichga o'tadi
      - {prefix}_cancel     — jarayonni bekor qiladi
    """
    sel_set = set(selected)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key, label in GENRES.items():
        mark = "✅" if key in sel_set else "▫️"
        row.append(
            InlineKeyboardButton(
                text=f"{mark} {label}",
                callback_data=f"{prefix}_tog:{key}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text=f"✅ Tasdiqlash ({len(sel_set)})",
                callback_data=f"{prefix}_done",
            ),
            InlineKeyboardButton(text="🚫 Bekor qilish", callback_data=f"{prefix}_cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def genre_picker_text(selected: list[str]) -> str:
    """Picker tepasidagi matn — tanlanganlar ro'yxatini ko'rsatadi."""
    if not selected:
        return (
            "🎭 <b>Janrlarni tanlang</b>\n"
            "<i>Bir yoki bir nechtasini tugmalar orqali belgilang, "
            'so\'ng "✅ Tasdiqlash" bosing.</i>'
        )
    labels = ", ".join(GENRES.get(k, k) for k in selected)
    return (
        "🎭 <b>Tanlangan janrlar:</b>\n"
        f"{labels}\n\n"
        "<i>Yana qo'shishingiz yoki olib tashlashingiz mumkin. "
        'Tayyor bo\'lsangiz "✅ Tasdiqlash".</i>'
    )
