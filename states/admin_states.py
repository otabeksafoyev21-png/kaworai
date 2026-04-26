from aiogram.fsm.state import State, StatesGroup


# 🔹 Anime qo‘shish
class AddAnime(StatesGroup):
    waiting_id = State()
    waiting_title = State()
    # Bir xil nomdagi kontent mavjud bo'lsa — fasl raqamini tanlash
    waiting_season = State()
    waiting_type = State()
    waiting_desc = State()
    waiting_genres = State()
    # `Psixologik` janri tanlangan bo'lsa — daraja (0..10) so'raladi
    waiting_psych_level = State()
    waiting_tags = State()
    waiting_mood = State()
    waiting_year = State()
    waiting_rating = State()
    waiting_total_episodes = State()
    waiting_duration = State()
    waiting_status = State()
    waiting_popularity = State()
    waiting_related = State()
    waiting_pro_lock = State()
    waiting_hidden_gem = State()
    waiting_poster = State()
    waiting_inline_url = State()
    waiting_trailer = State()


# 🔹 Kanal qo‘shish
class AddChannel(StatesGroup):
    waiting_name = State()
    waiting_url = State()
    waiting_type = State()
    waiting_channel_id = State()
    # Majburiy kanal uchun: "hamma viloyat" yoki "bitta viloyat" tanlash,
    # keyin agar bitta bo'lsa — region tanlash.
    waiting_region_scope = State()
    waiting_region_pick = State()


# 🔹 Anime tahrirlash
class EditAnime(StatesGroup):
    waiting_anime_id = State()
    waiting_field = State()
    waiting_value = State()
    picking_genres = State()
    waiting_episode_select = State()
    waiting_episode_video = State()
    waiting_delete_anime_id = State()
    waiting_delete_ep_anime_id = State()
    waiting_delete_ep_from = State()
    waiting_delete_ep_to = State()


# 🔹 Broadcast (xabar yuborish)
class BroadcastState(StatesGroup):
    waiting_content = State()
    waiting_media_type = State()
    waiting_caption = State()
    waiting_confirm = State()
    waiting_anime_id = State()
    waiting_anime_media_type = State()
    waiting_anime_post_caption = State()
    # Anime post uchun qo'shimcha button (Ko'rish dan oldin)
    waiting_anime_post_extra_btn_choice = State()
    waiting_anime_post_extra_btn_text = State()
    waiting_anime_post_extra_btn_url = State()
    waiting_anime_post_confirm = State()
    waiting_genre_name = State()
    waiting_genre_channel = State()
    # Kanalga maxsus xabar (rasm/video + button bilan)
    waiting_ch_content = State()
    waiting_ch_btn_choice = State()
    waiting_ch_btn_text = State()
    waiting_ch_btn_url = State()
    waiting_ch_channel_pick = State()
    # Foydalanuvchilarga xabar yuborishda region filteri
    waiting_users_region = State()


# 🔹 Baza zaxira (export/import) — filterli versiya
class BackupState(StatesGroup):
    waiting_restore_file = State()
    # Eksport filterlari
    waiting_export_ids = State()
    # Tiklash filterlari (ZIP yuklangandan keyin)
    waiting_restore_filter = State()
    waiting_restore_ids = State()


# 🔹 PRO tizim
class AdminProState(StatesGroup):
    waiting_user_id = State()
    waiting_pro_days = State()
    waiting_msg_text = State()
    # Yangi admin qo'shayotganda ruxsatlarni tugmalar orqali tanlash
    waiting_admin_perms = State()


# 🔹 Episode qo‘shish — bot orqali birma-bir yoki bulk (auto-detect)
class AddEpisodeState(StatesGroup):
    waiting_anime_id = State()
    waiting_from_ep = State()
    waiting_to_ep = State()
    # Bot orqali qo'shishning ikki rejimi uchun
    waiting_mode = State()  # inline: one-by-one yoki bulk
    waiting_single_video = State()  # birma-bir: har bir qism uchun video
    waiting_bulk_videos = State()  # bulk: videolarni yig'ish
    waiting_bulk_manual_ep = State()  # caption'dan qism raqami topilmasa qo'lda so'rash
    waiting_bulk_confirm = State()  # tasdiqlash ekrani
    waiting_filter = State()  # qismlar qo'shilgandan keyin ixtiyoriy filter (rasm/video/link)


# 🔹 Reklama boshqaruv
class AddAdState(StatesGroup):
    waiting_text = State()
    waiting_url = State()


# 🔹 Pro yozuv boshqaruv — admin Pro reklama matnini o'zgartirishi
class EditProTextState(StatesGroup):
    waiting_text = State()
