"""
Konfiguratsiya — barcha maxfiy qiymatlar .env orqali yuklanadi.

Avval bu faylda PAYMENT_CHANNEL_ID, CARD_NUMBER, CARD_OWNER va ADMIN_USERNAME
uchun to'g'ridan-to'g'ri kodda qattiq yozilgan qiymatlar bor edi. Bu xavfli:
karta raqami va qabul qiluvchi ismi PII/moliya ma'lumoti hisoblanadi va hech
qachon git repozitoriyasiga tushmasligi kerak. Endi ularning hammasi .env
fayli orqali sozlanadi.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Asosiy ───────────────────────────────────────────────
    BOT_TOKEN: str
    ADMIN_ID: int
    DATABASE_URL: str
    REDIS_URL: str

    # ── Kanallar ─────────────────────────────────────────────
    SECRET_CHANNEL_ID: int = 0
    # Tur bo'yicha alohida maxfiy kanallar (anime/dorama/serial/kino).
    # 0 bo'lsa SECRET_CHANNEL_ID ga fallback.
    SECRET_ANIME_CHANNEL_ID: int = 0
    SECRET_DORAMA_CHANNEL_ID: int = 0
    SECRET_SERIAL_CHANNEL_ID: int = 0
    SECRET_KINO_CHANNEL_ID: int = 0
    NEWS_CHANNEL_ID: int = 0
    PREVIEW_CHANNEL_ID: int = 0
    # Kunlik ZIP zaxira yuboriladigan kanal. 0 bo'lsa — kunlik zaxira o'chiq.
    BACKUP_CHANNEL_ID: int = 0

    # ── Pro to'lov tizimi (.env orqali majburiy) ─────────────
    PAYMENT_CHANNEL_ID: int
    CARD_NUMBER: str
    CARD_OWNER: str
    ADMIN_USERNAME: str

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


config = Settings()
