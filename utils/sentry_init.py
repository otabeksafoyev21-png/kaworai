"""
Sentry integratsiyasi (ixtiyoriy).

Nega kerak:
  - 200k user scale'da log fayllarini qo'lda kuzatish real emas —
    xatolar real vaqtda Sentry'ga yuborilsa, admin darrov ko'radi.
  - Global error handler xatolarni adminga Telegram xabari orqali
    yuboradi, lekin bu qisqa va traceback'ning to'liq qismi ko'rinmaydi.
    Sentry esa traceback, breadcrumbs va event context'ni to'liq saqlaydi.

Qanday yoqiladi:
  - SENTRY_DSN env var'ini qo'ying (masalan Railway Variables).
  - Paket: `sentry-sdk` requirements.txt'da bor.
  - DSN bo'lmasa — hech narsa qilmaydi (no-op), bot oddiy ishlaydi.

Loglarga Sentry hooking qilmaydi (aks holda har bir INFO log event bo'lib
ketardi). Faqat `logging.ERROR`/`logging.CRITICAL` va tutilmagan
istisnolar Sentry'ga yuboriladi.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def init_sentry() -> bool:
    """
    Sentry'ni ishga tushiradi (agar SENTRY_DSN o'rnatilgan bo'lsa).

    Qaytaradi:
        True — Sentry ulandi.
        False — DSN yo'q yoki init'da xato bo'ldi (bot bari bir ishlaydi).
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("Sentry: SENTRY_DSN yo'q, monitoring o'chiq")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logger.warning("Sentry: sentry-sdk paketi o'rnatilmagan, monitoring o'chiq")
        return False

    # Logging integratsiyasi — faqat ERROR va undan yuqori darajadagi
    # loglar Sentry'ga event bo'lib ketadi; INFO/DEBUG shovqin
    # yig'ilmaydi (aks holda Sentry quota tez tugaydi).
    logging_integration = LoggingIntegration(
        level=logging.INFO,  # breadcrumbs uchun
        event_level=logging.ERROR,  # Sentry event'lar uchun
    )

    environment = os.getenv("SENTRY_ENVIRONMENT", "production").strip() or "production"

    # Release tag — Railway deploy'da qaysi commit ishlab turganini bilish
    # uchun muhim. Aniq qiymat SENTRY_RELEASE'dan olinadi; bo'sh bo'lsa
    # Railway va GitHub Actions avtomatik beradigan env var'lardan birini
    # qidiramiz. Hech biri bo'lmasa — release qo'yilmaydi (Sentry o'zi
    # "unknown"ni belgilaydi).
    release = (
        (os.getenv("SENTRY_RELEASE", "") or "").strip()
        or (os.getenv("RAILWAY_GIT_COMMIT_SHA", "") or "").strip()
        or (os.getenv("GIT_COMMIT", "") or "").strip()
        or None
    )
    if release:
        # Qisqartiramiz — to'liq SHA bezakli ko'rinmaydi.
        release = release[:12]

    # traces_sample_rate — performance monitoring (0.0 = o'chiq).
    # 200k user'da barcha tranzaksiyalarni kuzatish quota'ni yeb qo'yadi.
    # Shuning uchun default 0 — kerak bo'lsa env orqali yoqiladi.
    try:
        traces_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0") or 0)
    except ValueError:
        traces_rate = 0.0

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release,
            integrations=[
                logging_integration,
                AsyncioIntegration(),
            ],
            traces_sample_rate=traces_rate,
            # send_default_pii=False — foydalanuvchi IP/id'si yuborilmasin.
            # Xatolar bot'da ichki tushunchalar bo'yicha ajratiladi, shaxsiy
            # ma'lumot kerak emas.
            send_default_pii=False,
            # attach_stacktrace — oddiy loglarda ham traceback qo'shsin,
            # shunda ERROR log'ini qayerdan kelganini bilish osonroq bo'ladi.
            attach_stacktrace=True,
            max_breadcrumbs=50,
        )
        logger.info("Sentry: ulanish OK (env=%s, traces=%.2f)", environment, traces_rate)
        return True
    except Exception:
        logger.exception("Sentry: init'da xato, monitoring o'chirildi")
        return False
