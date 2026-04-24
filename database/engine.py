import os
import re
import sys

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

load_dotenv()


def _mask_db_url(url: str) -> str:
    """Parolni yashirib URL'ni qaytaradi — deploy logida ko'rsatish uchun."""
    return re.sub(r"(://[^:@/]+):([^@/]+)@", r"\1:***@", url)


# Railway Postgres plugin standart ravishda `DATABASE_URL` degan o'zgaruvchi
# beradi. Loyiha faqat shu nomdan o'qiydi — eski `DB_URL` qo'llab-quvvatlanmaydi.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# ── Xato holatlarni erta (modul yuklanishida) ushlaydi ──
# Shunday qilib Railway log'ida aniq sabab ko'rinadi, `localhost:8080`'ga
# ulanib jim yiqilmaydi.
if not DATABASE_URL:
    print(
        "--- [FATAL] DATABASE_URL muhit o'zgaruvchisi bo'sh yoki qo'yilmagan.\n"
        "Railway'da Postgres plugin qo'shib, kaworai_bot servisiga DATABASE_URL\n"
        "o'zgaruvchisini Reference qilib qo'ying (${{Postgres.DATABASE_URL}}) yoki\n"
        "qo'lda `postgresql://user:pass@postgres.railway.internal:5432/railway` yozing.",
        file=sys.stderr,
    )
    raise SystemExit(1)

# Railway/Heroku reference syntaksi hal bo'lmasdan qolgan bo'lsa —
# `${{Postgres.DATABASE_URL}}` kabi matn kelsa — aniq xato yozib tushamiz.
if "${" in DATABASE_URL or "{{" in DATABASE_URL:
    print(
        f"--- [FATAL] DATABASE_URL hal qilinmagan Railway reference'iga o'xshaydi:\n"
        f"    {DATABASE_URL}\n"
        "Bu Postgres servisi boshqacha nomlanganligidan bo'lishi mumkin. Reference\n"
        "o'rniga Postgres servisidan to'liq URL'ni nusxalab qo'ying yoki servis\n"
        "nomini tekshiring.",
        file=sys.stderr,
    )
    raise SystemExit(1)

# Sxema prefiksini normallashtirish:
#   postgres://...          → postgresql+asyncpg://...
#   postgresql://...        → postgresql+asyncpg://...
#   postgresql+asyncpg://.. → shu holicha
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgres://") :]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgresql://") :]

# Qaysi DB'ga ulanayotganimizni ko'rsatamiz (parol yashirilgan).
print(f"--- [INFO] DATABASE_URL: {_mask_db_url(DATABASE_URL)}")

Base = declarative_base()

# ── Connection pool — Railway Postgres limit'iga mos ──
# Railway free/hobby tier'da Postgres odatda 25 ta connection bilan cheklangan.
# Agar pool_size + max_overflow > 25 bo'lsa, burst paytida (masalan 200k user
# broadcast'da yoki har daqiqa ishlaydigan reengagement loop'da) bot
# `FATAL: remaining connection slots are reserved` bilan yiqiladi.
#
# Default qiymatlar (Railway-safe): pool_size=10, max_overflow=5 → jami 15.
# Shunda Postgres tomon admin/plugin uchun 10 slot qolishi mumkin.
# Agar boshqa plan (Pro) ishlatilsa — `DB_POOL_SIZE` va `DB_MAX_OVERFLOW`
# env var'lari orqali sozlanadi, kod o'zgartirilmaydi.
#
# `pool_timeout` — agar barcha connection band bo'lsa, shuncha soniya
# kutib keyin xato qaytaradi (aks holda coroutine cheksiz osilib qoladi).
# `pool_recycle=1800` — har 30 daqiqada connection yangilanadi (Postgres
# idle timeout / proxy timeout'dan oldin).


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except ValueError:
        return default


DB_POOL_SIZE = _int_env("DB_POOL_SIZE", 10)
DB_MAX_OVERFLOW = _int_env("DB_MAX_OVERFLOW", 5)
DB_POOL_TIMEOUT = _int_env("DB_POOL_TIMEOUT", 30)
DB_POOL_RECYCLE = _int_env("DB_POOL_RECYCLE", 1800)

print(
    f"--- [INFO] DB pool: size={DB_POOL_SIZE} overflow={DB_MAX_OVERFLOW} "
    f"timeout={DB_POOL_TIMEOUT}s recycle={DB_POOL_RECYCLE}s"
)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def db_ping() -> bool:
    """
    Baza bilan ulanishni tez tekshiradi (healthcheck uchun).

    Qaytaradi:
        True — `SELECT 1` muvaffaqiyatli o'tdi.
        False — ulanish xatosi yoki timeout.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# Barcha migration — mavjud bo'lsa o'tkazib yuboradi
MIGRATIONS = [
    # Animes — eski ustunlar
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS total_episodes INTEGER DEFAULT 0",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS inline_thumbnail_url VARCHAR(500)",
    # Animes — Pro ustunlar
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS content_type VARCHAR(20) DEFAULT 'anime'",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]'",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS mood JSONB DEFAULT '[]'",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS episodes_count INTEGER",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS duration INTEGER",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'ongoing'",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS popularity FLOAT DEFAULT 0.0",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS popularity_score FLOAT DEFAULT 0.0",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS is_hidden_gem BOOLEAN DEFAULT FALSE",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS is_pro_locked BOOLEAN DEFAULT FALSE",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS added_by_id BIGINT",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS added_by_username VARCHAR(100)",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS added_at TIMESTAMP DEFAULT NOW()",
    # Users — Pro ustunlar
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_pro BOOLEAN DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_until TIMESTAMP",
    # Admins — qo'shimcha
    "ALTER TABLE admins ADD COLUMN IF NOT EXISTS added_by BIGINT",
    "ALTER TABLE admins ADD COLUMN IF NOT EXISTS added_at TIMESTAMP DEFAULT NOW()",
    # Admins — per-admin ruxsatlar (PR #19, JSON list: ["add_anime", ...])
    "ALTER TABLE admins ADD COLUMN IF NOT EXISTS permissions JSON",
    # Users — viloyat (region) kodini saqlash (PR #24, O'zbekiston 14 viloyat)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS region VARCHAR(40)",
    # Channels — majburiy kanallar uchun region cheklovi (PR #24)
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS region VARCHAR(40)",
    # Hamkor (partner) tizimi — kim qo'shgani (NULL = bosh admin / global)
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS owner_id BIGINT",
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS owner_id BIGINT",
    "CREATE INDEX IF NOT EXISTS ix_animes_owner ON animes(owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_channels_owner ON channels(owner_id)",
    # Anime — psixologik daraja (0..10) va fasl raqami
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS psychological_level INTEGER",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS season INTEGER DEFAULT 1",
    # Users — re-engagement (qayta-faollashtirish) uchun maydonlar
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP DEFAULT NOW()",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS reminder_stage INTEGER DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS ix_users_last_active ON users(last_active)",
    # Users — Pro UX rejimi (edit=silliq tahrir, send=har safar yangi xabar)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS ux_mode VARCHAR(10) DEFAULT 'edit'",
    # Users — Pro /start menyu qo'shimcha tugmalar (JSON list, Pro shortcut keys)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS start_extras JSONB DEFAULT '[]'",
    # AnimeSubscription jadvali
    """
    CREATE TABLE IF NOT EXISTS anime_subscriptions (
        id         SERIAL PRIMARY KEY,
        anime_id   INTEGER NOT NULL REFERENCES animes(id) ON DELETE CASCADE,
        user_id    BIGINT  NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE (anime_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_anisub_anime ON anime_subscriptions(anime_id)",
    "CREATE INDEX IF NOT EXISTS ix_anisub_user  ON anime_subscriptions(user_id)",
    # related_content jadvali
    """
    CREATE TABLE IF NOT EXISTS related_content (
        id            SERIAL PRIMARY KEY,
        anime_id      INTEGER NOT NULL REFERENCES animes(id) ON DELETE CASCADE,
        related_id    INTEGER NOT NULL REFERENCES animes(id) ON DELETE CASCADE,
        relation_type VARCHAR(20) DEFAULT 'similar'
    )
    """,
    # user_watch_history jadvali
    """
    CREATE TABLE IF NOT EXISTS user_watch_history (
        id           SERIAL PRIMARY KEY,
        user_id      BIGINT  NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        anime_id     INTEGER NOT NULL REFERENCES animes(id) ON DELETE CASCADE,
        watched_at   TIMESTAMP DEFAULT NOW(),
        last_episode INTEGER DEFAULT 1,
        is_completed BOOLEAN DEFAULT FALSE
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_wh_user  ON user_watch_history(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_wh_anime ON user_watch_history(anime_id)",
    # view_records jadvali
    """
    CREATE TABLE IF NOT EXISTS view_records (
        id        SERIAL PRIMARY KEY,
        anime_id  INTEGER NOT NULL REFERENCES animes(id) ON DELETE CASCADE,
        user_id   BIGINT,
        viewed_at TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_vr_anime ON view_records(anime_id)",
    # user_taste_profiles jadvali
    """
    CREATE TABLE IF NOT EXISTS user_taste_profiles (
        id              SERIAL PRIMARY KEY,
        user_id         BIGINT NOT NULL UNIQUE REFERENCES users(telegram_id) ON DELETE CASCADE,
        fav_genres      JSONB DEFAULT '{}',
        fav_tags        JSONB DEFAULT '{}',
        fav_moods       JSONB DEFAULT '{}',
        fav_type        VARCHAR(20),
        avg_rating_pref FLOAT DEFAULT 7.0,
        updated_at      TIMESTAMP DEFAULT NOW()
    )
    """,
]


async def init_db():
    try:
        # Avval modellarni import qilish (jadval yaratish uchun)

        async with engine.begin() as conn:
            # SQLAlchemy modellari orqali asosiy jadvallarni yaratish
            await conn.run_sync(Base.metadata.create_all)

            # Qo'shimcha migration — xato bo'lsa o'tkazib yuboradi
            for migration in MIGRATIONS:
                migration = migration.strip()
                if not migration:
                    continue
                try:
                    await conn.execute(text(migration))
                except Exception as e:
                    err = str(e).lower()
                    # Allaqachon mavjud bo'lsa — normal holat
                    if any(x in err for x in ("already exists", "duplicate", "already")):
                        pass
                    else:
                        print(f"[MIGRATION WARNING] {str(e)[:80]}")

        print("--- [INFO] Baza jadvallari tayyor! ---")

    except Exception as e:
        print(f"--- [ERROR] Bazada xato: {e} ---")
        raise
