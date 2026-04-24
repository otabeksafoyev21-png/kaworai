"""
Oddiy /healthz HTTP endpoint — orchestrator (Railway, K8s) uchun.

Nega kerak:
  - Railway bot konteyner ishlayaptimi yoki yo'qmi bilmaydi. Agar
    bot polling'da "silently" yiqilgan bo'lsa (masalan Telegram bilan
    TLS uzilgan va coroutine deadlock bo'lgan), orchestrator buni
    bilmaydi va qayta deploy qilmaydi.
  - /healthz har safar 200 OK qaytaradi — orchestrator shu endpoint'ni
    probe qilib, bot hayotimi yo'qmi aniqlaydi.
  - Endpoint alohida portda (default 8080) — bot polling'ga aralashmaydi.

Ishlatish:
  - HEALTHCHECK_PORT env var (default 8080).
  - HEALTHCHECK_PORT=0 qo'yilsa — server ishga tushmaydi (o'chiq).
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

logger = logging.getLogger(__name__)

# DB ping uchun cheklov: agar baza 2 soniya ichida javob bermasa —
# "down" deb hisoblaymiz va 503 qaytaramiz. Orchestrator qayta deploy
# qilsa, baza ham tiklangan bo'lishi kerak.
_DB_PING_TIMEOUT_SEC = 2.0


async def _healthz(_request: web.Request) -> web.Response:
    """
    Bot + baza tirikligini tekshiradi.

    Baza ping muvaffaqiyatli bo'lsa — 200 OK `ok`.
    Baza javob bermasa yoki xato bo'lsa — 503 `db_down`.

    Bu Railway/K8s probe uchun muhim: agar faqat `ok` qaytarsak, baza
    o'chgan bo'lsa ham bot "tirik" ko'rinadi va orchestrator qayta deploy
    qilmaydi. DB ping qo'shilganda — baza uzilganda ham aniq signal bor.
    """
    # Lazy import — healthcheck moduli database'ga bog'lanib qolmasligi uchun
    # (unit test'larda yoki database yo'q bo'lsa ham ishlaydi).
    try:
        from database.engine import db_ping
    except Exception:
        return web.Response(text="ok", content_type="text/plain")

    try:
        alive = await asyncio.wait_for(db_ping(), timeout=_DB_PING_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("healthcheck: DB ping timeout (%.1fs)", _DB_PING_TIMEOUT_SEC)
        return web.Response(status=503, text="db_timeout", content_type="text/plain")
    except Exception:
        logger.exception("healthcheck: DB ping unexpected error")
        return web.Response(status=503, text="db_error", content_type="text/plain")

    if not alive:
        return web.Response(status=503, text="db_down", content_type="text/plain")
    return web.Response(text="ok", content_type="text/plain")


async def start_healthcheck_server() -> web.AppRunner | None:
    """
    aiohttp health-check serverini ishga tushiradi (background'da).

    Qaytaradi:
        AppRunner — server obyekti (bot to'xtaganda cleanup uchun).
        None — server o'chirilgan yoki ishga tushmadi.
    """
    try:
        port = int(os.getenv("HEALTHCHECK_PORT", "8080") or 0)
    except ValueError:
        logger.warning("healthcheck: HEALTHCHECK_PORT noto'g'ri, server o'chiq")
        return None

    if port <= 0:
        logger.info("healthcheck: HEALTHCHECK_PORT=0, server o'chiq")
        return None

    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/", _healthz)  # Railway ba'zan `/` ni probe qiladi

    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=port)
        await site.start()
        logger.info("healthcheck: /healthz ishga tushdi (port=%d)", port)
        return runner
    except OSError as e:
        # Port band bo'lsa (masalan lokal dev'da bir nechta instance) —
        # faqat warning, bot ishga tushaverishi kerak.
        logger.warning("healthcheck: port %d band yoki xato: %s", port, e)
        try:
            await runner.cleanup()
        except Exception:
            pass
        return None
    except Exception:
        logger.exception("healthcheck: ishga tushirishda kutilmagan xato")
        try:
            await runner.cleanup()
        except Exception:
            pass
        return None
