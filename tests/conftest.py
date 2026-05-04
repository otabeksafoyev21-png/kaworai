"""Pytest umumiy sozlamalar.

Loyihada `database/engine.py` modul yuklanishidayoq `DATABASE_URL` env-var'ni
talab qiladi (yo'q bo'lsa `SystemExit(1)`). Test paytida haqiqiy Postgres
yo'q, shuning uchun import-vaqti uchun fake URL qo'yamiz. Hech qanday
test haqiqatda baza bilan ulanmaydi — faqat regex va sof Python yordamchilarni
sinaymiz.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Repo root'ni sys.path'ga qo'shamiz — `from handlers... import` ishlasin.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Modul yuklanishidagi env-var validatsiyalarini chetlab o'tish — har bir
# muhit o'zgaruvchisiga zararsiz placeholder qiymat beramiz. Test paytida
# hech bir tarmoq chaqiruvi qilinmaydi (faqat sof Python regex/yordamchilar).
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("BOT_TOKEN", "0000000000:TEST")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("NEWS_CHANNEL_ID", "0")
os.environ.setdefault("SECRET_CHANNEL_ID", "0")
