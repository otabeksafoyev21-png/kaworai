import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from data import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  FSM STORAGE
# ─────────────────────────────────────────────────────────────
# Oldin Redis ishlatilardi (`RedisStorage.from_url(...)`). Railway'da
# ichki Redis ba'zida sekin yoki javob bermaydi; u holda HAR BIR tugma
# bosish FSM state o'qishida jimgina osilib qoladi va admin panel
# "ishlamayapti" deb ko'rinadi (aynan foydalanuvchi shikoyat qilgan holat:
# `/admin` ochiladi, lekin keyingi tugmalar javobsiz qoladi).
#
# Endi default MemoryStorage ishlatiladi — Redis ulanish muammolari
# butunlay chetlab o'tiladi. Tradeoff: bot restart bo'lsa faqat hozirgi
# faol FSM flow'lar yo'qoladi (admin anime qo'shish yarmida va hokazo).
# 200k user uchun bu xavfsiz — FSM state har bir foydalanuvchi uchun
# qisqa muddatli (soniyalar/daqiqalar).
#
# Agar Redis kerak bo'lsa: `USE_REDIS_FSM=1` env var qo'yiladi — shundagina
# Redis ishlatiladi. Default holat — MemoryStorage.

storage: MemoryStorage

_use_redis = os.getenv("USE_REDIS_FSM", "").strip().lower() in ("1", "true", "yes")
if _use_redis and config.REDIS_URL:
    try:
        from aiogram.fsm.storage.redis import RedisStorage

        storage = RedisStorage.from_url(config.REDIS_URL)
        logger.info("FSM storage: RedisStorage (USE_REDIS_FSM=1)")
    except Exception:
        logger.exception("FSM storage: RedisStorage init failed, falling back to MemoryStorage")
        storage = MemoryStorage()
else:
    storage = MemoryStorage()
    logger.info("FSM storage: MemoryStorage (default; set USE_REDIS_FSM=1 to use Redis)")

# Bot va Dispatcher
#
# Telegram API ba'zida sekin javob beradi (10+ soniya). Agar timeout
# bo'lmasa, bitta sekin so'rov butun coroutine'ni bloklab qo'yadi va
# 200k foydalanuvchi scale'ida task'lar to'planib ketadi. Shuning uchun
# `AiohttpSession(timeout=...)` bilan qattiq vaqt chegarasi qo'yamiz.
# 60 soniya — odatdagi uzun so'rovlar (masalan `getChatMember` sekin
# kanalda) uchun yetarli, lekin cheksiz kutishdan himoyalaydi.
session = AiohttpSession(timeout=60)

bot = Bot(
    token=config.BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=storage)

# BU YERDA 'db' YO'Q, chunki u database/engine.py da
