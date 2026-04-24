"""
models.py ga QO'SHISH kerak bo'lgan yangi model va migration SQL.

1. AnimeSubscription klassini models.py ga qo'shing
2. Migration SQL ni bazaga ishga tushiring
"""

# ── models.py ga qo'shing ─────────────────────────────────────

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer
from sqlalchemy.sql import func

# from database.engine import Base  # ← mavjud import


class AnimeSubscription:
    """
    User animega obuna.
    Yangi qism qo'shilganda obuna bo'lgan userlarga xabar ketadi.
    """

    __tablename__ = "anime_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    anime_id = Column(Integer, ForeignKey("animes.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"))
    created_at = Column(DateTime, server_default=func.now())


# ── Migration SQL ─────────────────────────────────────────────

MIGRATION_SQL = """
-- AnimeSubscription jadval
CREATE TABLE IF NOT EXISTS anime_subscriptions (
    id         SERIAL PRIMARY KEY,
    anime_id   INTEGER    NOT NULL REFERENCES animes(id) ON DELETE CASCADE,
    user_id    BIGINT     NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    created_at TIMESTAMP  DEFAULT NOW(),
    UNIQUE (anime_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_anisub_anime ON anime_subscriptions(anime_id);
CREATE INDEX IF NOT EXISTS ix_anisub_user  ON anime_subscriptions(user_id);

-- animes jadvaliga added_by maydonlari (kim qo'shgan)
ALTER TABLE animes
    ADD COLUMN IF NOT EXISTS added_by_id       BIGINT      NULL,
    ADD COLUMN IF NOT EXISTS added_by_username VARCHAR(100) NULL,
    ADD COLUMN IF NOT EXISTS added_at          TIMESTAMP   DEFAULT NOW();
"""

# ── Ishga tushirish (migration.py ga qo'shing yoki alohida) ──

import asyncio
import os

import asyncpg


async def run_migration():
    db_url = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/kaworai")
    conn = await asyncpg.connect(dsn=db_url)
    try:
        stmts = [s.strip() for s in MIGRATION_SQL.split(";") if s.strip() and not s.strip().startswith("--")]
        for stmt in stmts:
            try:
                await conn.execute(stmt)
                print(f"✅ {stmt[:60]}...")
            except Exception as e:
                if "already exists" in str(e).lower():
                    pass
                else:
                    print(f"⚠️ {str(e)[:80]}")
        print("\n✅ Migration tugadi!")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())
