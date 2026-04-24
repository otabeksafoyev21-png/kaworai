"""
Migration v2 — yangi ustunlar qo'shish
Ishga tushirish: python migration_v2.py

Qo'shiladi:
  - anime_subscriptions (yangi jadval)
  - animes.added_by_id, added_by_username, added_at
  - admins.added_by, added_at
"""

import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

SQL_STATEMENTS = [
    # anime_subscriptions yangi jadval
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
    # animes — kim qo'shgan
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS added_by_id       BIGINT       NULL",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS added_by_username VARCHAR(100) NULL",
    "ALTER TABLE animes ADD COLUMN IF NOT EXISTS added_at          TIMESTAMP    DEFAULT NOW()",
    # admins — kim qo'shgan, qachon
    "ALTER TABLE admins ADD COLUMN IF NOT EXISTS added_by  BIGINT    NULL",
    "ALTER TABLE admins ADD COLUMN IF NOT EXISTS added_at  TIMESTAMP DEFAULT NOW()",
    # admins — per-admin ruxsatlar (JSON list)
    "ALTER TABLE admins ADD COLUMN IF NOT EXISTS permissions JSON NULL",
]


async def run():
    db_url = os.getenv("DATABASE_URL", "").strip()
    # asyncpg.connect `postgresql+asyncpg://` ni qabul qilmaydi — SQLAlchemy
    # driver prefiksi bo'lsa olib tashlaymiz.
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    # Railway `postgres://` variantini ham beradi — asyncpg buni qabul qiladi.

    if not db_url:
        print("❌ DATABASE_URL .env da topilmadi!")
        return

    print(f"🔌 Ulanilmoqda: {db_url[:40]}...")
    conn = await asyncpg.connect(dsn=db_url)

    try:
        ok = 0
        skip = 0
        for stmt in SQL_STATEMENTS:
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                await conn.execute(stmt)
                short = stmt.replace("\n", " ").strip()[:70]
                print(f"  ✅ {short}")
                ok += 1
            except Exception as e:
                err = str(e).lower()
                if any(x in err for x in ("already exists", "duplicate", "does not exist")):
                    skip += 1
                else:
                    print(f"  ⚠️  {str(e)[:100]}")

        print(f"\n✅ Migration tugadi! ({ok} bajarildi, {skip} o'tkazib yuborildi)")
        print("\nEndi models.py ga quyidagi ustunlarni qo'shing (migration dan keyin):")
        print("  Admin: added_by = Column(BigInteger, nullable=True)")
        print("  Admin: added_at = Column(DateTime, server_default=func.now())")
        print("  Anime: added_by_id, added_by_username, added_at")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
