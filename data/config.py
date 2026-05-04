"""
data/config.py — environs yordamida .env qiymatlarni yuklaydi.

`ADMINS` bo'sh stringlardan tozalanib qaytariladi, aks holda bo'sh
`ADMIN_ID` qiymat "admin" ro'yxatiga bo'sh satr qo'shib, avtorizatsiya
mantig'ini buzilishiga olib keladi.
"""

from environs import Env

env = Env()
env.read_env()


def _parse_admin_ids(raw: str) -> list[str]:
    """Verguldan ajratilgan ID ro'yxatini tozalaydi va bo'sh bo'laklarni tashlaydi."""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


BOT_TOKEN = env.str("BOT_TOKEN")
ADMINS = _parse_admin_ids(env.str("ADMIN_ID", ""))

# ADMIN_ID — asosiy owner. Bir nechta admin bo'lsa — birinchisini olamiz.
ADMIN_ID = int(ADMINS[0]) if ADMINS else 0

DATABASE_URL = env.str("DATABASE_URL")
REDIS_URL = env.str("REDIS_URL")
NEWS_CHANNEL_ID = env.int("NEWS_CHANNEL_ID")
SECRET_CHANNEL_ID = env.int("SECRET_CHANNEL_ID")
# Tur bo'yicha alohida maxfiy kanallar — auto-add'da kontent turiga qarab
# kerakli kanalga yuborish uchun. Agar tegishli kanal 0 bo'lsa,
# `SECRET_CHANNEL_ID` ga fallback bo'ladi.
SECRET_ANIME_CHANNEL_ID = env.int("SECRET_ANIME_CHANNEL_ID", 0)
SECRET_DORAMA_CHANNEL_ID = env.int("SECRET_DORAMA_CHANNEL_ID", 0)
SECRET_SERIAL_CHANNEL_ID = env.int("SECRET_SERIAL_CHANNEL_ID", 0)
SECRET_KINO_CHANNEL_ID = env.int("SECRET_KINO_CHANNEL_ID", 0)
# Bot orqali qism qo'shishda preview/stage kanal (admin ko'rib tasdiqlash uchun).
# Agar 0 bo'lsa — preview bot DM'ida yuboriladi.
PREVIEW_CHANNEL_ID = env.int("PREVIEW_CHANNEL_ID", 0)
# Kunlik ZIP zaxira yuboriladigan kanal. 0 bo'lsa — kunlik zaxira o'chiq.
BACKUP_CHANNEL_ID = env.int("BACKUP_CHANNEL_ID", 0)

# Pro to'lov tizimi — maxfiy PII/moliya ma'lumotlari
# Bularning hammasi .env orqali taqdim etilishi kerak.
PAYMENT_CHANNEL_ID = env.int("PAYMENT_CHANNEL_ID", 0)
CARD_NUMBER = env.str("CARD_NUMBER", "")
CARD_OWNER = env.str("CARD_OWNER", "")
ADMIN_USERNAME = env.str("ADMIN_USERNAME", "")
