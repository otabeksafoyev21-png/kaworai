import asyncio
import logging
import os
import signal
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from data.config import BACKUP_CHANNEL_ID
from database.engine import engine, init_db
from handlers.admin import admin_router
from handlers.admin_pro import pro_admin_router
from handlers.callbacks import callback_router
from handlers.errors import error_router
from handlers.genres import genre_router
from handlers.inline import inline_router
from handlers.pro_payment import pro_payment_router
from handlers.users import user_router
from handlers.users_pro import pro_user_router
from loader import bot, dp
from middlewares.subscription import SubscriptionMiddleware
from middlewares.throttling import ThrottlingMiddleware
from utils.daily_backup import daily_backup_loop
from utils.healthcheck import start_healthcheck_server
from utils.logging_setup import setup_logging
from utils.reengagement import reengagement_loop, reengagement_router
from utils.sentry_init import init_sentry

logger = logging.getLogger(__name__)

# Fon task'larga reference — asyncio.create_task natijasini shu yerda saqlaymiz,
# aks holda GC ularni erta to'xtatib qo'yishi mumkin (RUF006).
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# Graceful shutdown signal — SIGTERM/SIGINT kelganda set bo'ladi,
# polling tsikli buni ko'rib yumshoq chiqadi.
_SHUTDOWN_EVENT: asyncio.Event | None = None


async def on_startup():
    logging.info("Ma'lumotlar bazasi jadvallari tekshirilmoqda...")
    try:
        await init_db()
        logging.info("Baza jadvallari tayyor.")
    except Exception as e:
        logging.error(f"Bazani yaratishda jiddiy xato: {e}")
        sys.exit(1)

    # Kunlik ZIP zaxira scheduler — alohida fon task sifatida ishlaydi.
    # BACKUP_CHANNEL_ID=0 bo'lsa loop o'zi darrov chiqadi (no-op).
    if BACKUP_CHANNEL_ID:
        # Task reference saqlanadi (RUF006) — GC tomonidan erta to'xtatilmasligi uchun.
        _BACKGROUND_TASKS.add(asyncio.create_task(daily_backup_loop(bot)))
        logging.info("Kunlik zaxira scheduler ishga tushirildi (kanal=%s)", BACKUP_CHANNEL_ID)
    else:
        logging.info("BACKUP_CHANNEL_ID=0 — kunlik zaxira o'chiq")

    # Re-engagement (qayta-faollashtirish) scheduler — har 24 soatda
    # uzoq vaqt faol bo'lmagan userlarga yumshoq eslatma yuboradi.
    _BACKGROUND_TASKS.add(asyncio.create_task(reengagement_loop(bot)))
    logging.info("Re-engagement scheduler ishga tushirildi")

    # Health-check HTTP serveri (Railway/K8s probe uchun). HEALTHCHECK_PORT=0
    # bo'lsa ishga tushmaydi — lokal dev'da muammo yo'q. Runner'ga modul
    # global'i orqali reference saqlaymiz — aks holda GC uni yig'ib
    # qo'yishi mumkin. Process tugaganda OS port'ni ozod qiladi.
    runner = await start_healthcheck_server()
    if runner is not None:
        globals()["_HEALTHCHECK_RUNNER"] = runner


async def _graceful_shutdown(reason: str) -> None:
    """
    Bot'ni yumshoq to'xtatish: polling uzilsin, keyin FSM, bot session va
    DB pool yopilsin. SIGTERM (Railway deploy) yoki KeyboardInterrupt
    paytida chaqiriladi. Sekvensiya:

      1. _SHUTDOWN_EVENT set — polling loop tugaydi.
      2. `dp.stop_polling()` — aiogram cheklangan vaqtda cleanup qiladi.
      3. Fon task'lar bekor qilinadi (daily backup, reengagement).
      4. FSM storage (Memory/Redis) yopiladi — holat yo'qotilmaydi.
      5. Bot session va DB pool yopiladi.

    Har bir qadam `except`'ga o'raldi — bitta xato qolganlarini to'xtatmasin.
    """
    logger.info("shutdown: boshlanmoqda (reason=%s)", reason)

    if _SHUTDOWN_EVENT is not None and not _SHUTDOWN_EVENT.is_set():
        _SHUTDOWN_EVENT.set()

    try:
        await dp.stop_polling()
    except Exception:
        logger.exception("shutdown: dp.stop_polling xato")

    # Fon task'lar (daily backup, reengagement) — cancel, keyin kutamiz.
    for task in list(_BACKGROUND_TASKS):
        if not task.done():
            task.cancel()
    if _BACKGROUND_TASKS:
        try:
            await asyncio.wait(_BACKGROUND_TASKS, timeout=5)
        except Exception:
            logger.exception("shutdown: fon task'larni kutishda xato")

    try:
        await dp.storage.close()
    except Exception:
        logger.exception("shutdown: FSM storage yopishda xato")

    runner = globals().get("_HEALTHCHECK_RUNNER")
    if runner is not None:
        try:
            await runner.cleanup()
        except Exception:
            logger.exception("shutdown: healthcheck cleanup xato")

    try:
        await bot.session.close()
    except Exception:
        logger.exception("shutdown: bot.session.close xato")

    try:
        await engine.dispose()
    except Exception:
        logger.exception("shutdown: DB engine.dispose xato")

    logger.info("shutdown: tugadi")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """SIGTERM/SIGINT signal handler'ni loop'ga o'rnatadi (faqat Unix'da)."""

    def _handler(signame: str) -> None:
        logger.info("signal: %s qabul qilindi, graceful shutdown", signame)
        loop.create_task(_graceful_shutdown(signame))

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handler, sig_name)
        except (NotImplementedError, RuntimeError):
            # Windows yoki ba'zi embedded Python'larda qo'llab-quvvatlanmaydi
            logger.debug("signal: %s handler o'rnatilmadi (platform)", sig_name)


async def main():
    # Sentry — logging va aiogram o'rnatilishidan oldin chaqirilishi kerak,
    # shunda init davridagi xatolar ham qamrab olinadi.
    init_sentry()

    # Structured logging: LOG_FORMAT=json bo'lsa JSON chiqaradi (Railway
    # log filterlash uchun), aks holda odatiy matn formati.
    setup_logging()

    global _SHUTDOWN_EVENT
    _SHUTDOWN_EVENT = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop())

    await on_startup()

    # Global xato handleri — BIRINCHI ulanishi kerak, shunda
    # boshqa routerlardagi tutilmagan istisnolar shu yerga tushadi.
    dp.include_router(error_router)

    # Routerlar (pro_payment ENG BIRINCHI — kawaii_pass ni ushlaydi)
    dp.include_router(pro_payment_router)
    dp.include_router(admin_router)
    dp.include_router(pro_admin_router)
    dp.include_router(pro_user_router)
    dp.include_router(user_router)
    dp.include_router(callback_router)
    dp.include_router(inline_router)
    dp.include_router(genre_router)
    dp.include_router(reengagement_router)

    # Throttling middleware — foydalanuvchi spam / brute-force qilishining
    # oldini oladi. Handler qabul qilinganidan oldin ishga tushadi, shuning
    # uchun uni subscription middleware'dan oldin qo'shamiz.
    dp.message.middleware(ThrottlingMiddleware())
    dp.callback_query.middleware(ThrottlingMiddleware())

    # Obuna tekshiruv middleware
    dp.message.middleware(SubscriptionMiddleware())
    dp.callback_query.middleware(SubscriptionMiddleware())

    bot_info = await bot.get_me()
    # Instance unique ID — agar Railway'da bir nechta bot instance ishlab
    # ketsa (yangi deploy eski konteynerni o'chirmagan), polling konflikt
    # bo'lib tugmalar "silently" yo'qoladi. Log'da har ishga tushgan
    # instance alohida PID + random ID bilan ko'rinadi — foydalanuvchi
    # Railway log'da shu qatorlar sonini ko'rib duplicate'ni aniqlaydi.
    import random as _rnd

    _instance_id = f"pid={os.getpid()}-rnd={_rnd.randint(1000, 9999)}"
    logging.info("--- BOT INSTANCE %s ISHGA TUSHDI ---", _instance_id)
    logging.info("USER: @%s  ID: %s", bot_info.username, bot_info.id)
    print(
        f"--- BOT ISHGA TUSHDI ---\nUSER: @{bot_info.username}\nID: {bot_info.id}\nINSTANCE: {_instance_id}\n------------------------"
    )

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        # start_polling() — signal handler `dp.stop_polling()` chaqirganda
        # o'zi normal ravishda tugaydi. handle_signals=False chunki bizda
        # allaqachon asyncio-darajali handler bor (_install_signal_handlers).
        await dp.start_polling(bot, handle_signals=False)
    except Exception as e:
        logging.error(f"Polling davomida xatolik: {e}")
    finally:
        # Agar shutdown hali chaqirilmagan bo'lsa (masalan polling o'zidan
        # tugadi), bir marta graceful_shutdown chaqiramiz — idempotent.
        if _SHUTDOWN_EVENT is None or not _SHUTDOWN_EVENT.is_set():
            await _graceful_shutdown("polling_exit")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot foydalanuvchi tomonidan to'xtatildi.")
