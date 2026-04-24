from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- XATOLIKNI TUZATISH UCHUN ---
# handlers/callbacks.py qidirayotgan obyektni yaratib qo'yamiz
next_episode_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Keyingi qism ▶", callback_data="next_ep")]]
)


def main_menu() -> InlineKeyboardMarkup:
    """/start da chiqadigan asosiy menu"""
    builder = InlineKeyboardBuilder()

    builder.row(InlineKeyboardButton(text="🔍 Janr bo‘yicha qidirish", callback_data="show_genres"))
    builder.row(
        InlineKeyboardButton(text="🎬 Barcha animelar", callback_data="list_anime_1"),
        InlineKeyboardButton(text="⭐ Eng mashhur", callback_data="top_anime"),
    )
    builder.row(InlineKeyboardButton(text="ℹ️ Yordam / Qo‘llanma", callback_data="help"))

    return builder.as_markup()


def genres_keyboard() -> InlineKeyboardMarkup:
    """Janr tanlash menyusi"""
    genres = [
        "Action",
        "Romance",
        "Comedy",
        "Fantasy",
        "Drama",
        "Horror",
        "Slice of Life",
        "Sci-Fi",
        "Sports",
        "Mystery",
        "Supernatural",
        "Mecha",
    ]

    builder = InlineKeyboardBuilder()

    for genre in genres:
        builder.button(text=genre, callback_data=f"genre_select:{genre.lower()}")

    builder.button(text="✅ Tanlashni tugatish", callback_data="genres_done")
    builder.button(text="◀ Orqaga", callback_data="main_menu")

    builder.adjust(3)
    return builder.as_markup()


def owner_admin_menu() -> InlineKeyboardMarkup:
    """To‘liq owner /admin menyusi"""
    builder = InlineKeyboardBuilder()

    builder.row(InlineKeyboardButton(text="➕ Yangi anime qo‘shish", callback_data="admin_add_anime"))
    builder.row(
        InlineKeyboardButton(text="✏️ Anime tahrirlash", callback_data="admin_edit_list"),
        InlineKeyboardButton(text="🗑 Anime o‘chirish", callback_data="admin_delete_list"),
    )
    builder.row(
        InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats"),
        InlineKeyboardButton(text="📤 Export (JSON)", callback_data="admin_export_json"),
    )
    builder.row(
        InlineKeyboardButton(text="👥 Hamkor qo‘shish", callback_data="admin_add_partner"),
        InlineKeyboardButton(text="📡 Majburiy kanallar", callback_data="admin_channels"),
    )
    builder.row(InlineKeyboardButton(text="📰 News kanal sozlash", callback_data="admin_news_channel"))
    builder.row(InlineKeyboardButton(text="◀ Chiqish", callback_data="admin_exit"))

    builder.adjust(1, 2, 2, 2, 1, 1)
    return builder.as_markup()


def partner_admin_menu() -> InlineKeyboardMarkup:
    """Hamkorlar uchun menyu"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Yangi anime qo‘shish", callback_data="admin_add_anime"))
    builder.row(InlineKeyboardButton(text="✏️ Anime tahrirlash", callback_data="admin_edit_list"))
    builder.row(InlineKeyboardButton(text="◀ Chiqish", callback_data="admin_exit"))
    return builder.as_markup()


def anime_list_pagination(page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Anime ro‘yxati uchun pagination"""
    builder = InlineKeyboardBuilder()

    if total_pages > 1:
        row = []
        if page > 1:
            row.append(InlineKeyboardButton(text="◀ Oldingi", callback_data=f"list_anime_{page - 1}"))
        row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(InlineKeyboardButton(text="Keyingi ▶", callback_data=f"list_anime_{page + 1}"))
        builder.row(*row)

    builder.row(InlineKeyboardButton(text="◀ Asosiy menyuga", callback_data="main_menu"))
    return builder.as_markup()


def anime_actions(anime_id: int, is_owner: bool = False) -> InlineKeyboardMarkup:
    """Anime boshqaruvi"""
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(text="◀ Oldingi qism", callback_data=f"ep_prev_{anime_id}"),
        InlineKeyboardButton(text="Keyingi qism ▶", callback_data=f"ep_next_{anime_id}"),
    )

    if is_owner:
        builder.row(
            InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"edit_anime_{anime_id}"),
            InlineKeyboardButton(text="🗑 O‘chirish", callback_data=f"delete_anime_{anime_id}"),
        )

    builder.row(InlineKeyboardButton(text="◀ Menyuga qaytish", callback_data="main_menu"))
    return builder.as_markup()


def confirm_delete(anime_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Ha, o‘chirish", callback_data=f"confirm_delete_{anime_id}"),
        InlineKeyboardButton(text="❌ Yo‘q, qaytish", callback_data=f"cancel_delete_{anime_id}"),
    )
    return builder.as_markup()
