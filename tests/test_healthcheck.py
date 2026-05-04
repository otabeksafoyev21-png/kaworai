"""Healthcheck `db_ping()` uchun engil test.

Test fake DATABASE_URL bilan ishlaydi (haqiqiy Postgres yo'q), shuning
uchun `db_ping()` `False` qaytarishi kerak — bu rejaga muvofiq xatti-harakat.
Asosiy maqsad: funksiya import qilinishi va xato bermay False qaytarishi.
"""

from __future__ import annotations

import asyncio

from database.engine import db_ping


def test_db_ping_returns_false_without_real_db() -> None:
    # Fake URL'ga ulanish muvaffaqiyatsiz bo'ladi — db_ping() False qaytaradi
    # (xato yutiladi). Bu /healthz endpoint'ining "DB tushdi" rejimini sinaydi.
    result = asyncio.run(db_ping())
    assert result is False
