"""
Re-engagement (qayta-faollashtirish) tizimi.

Bu modul ikki qismdan iborat:

1. **Scheduler** (`reengagement_loop`) — har sutkada bir marta ishga tushib,
   uzoq vaqt faol bo'lmagan foydalanuvchilarga yumshoq eslatma yuboradi.
   Bosqichlar (progressive escalation):

       stage 0 — hali eslatma yuborilmagan.
       stage 1 — 21+ kun faol emas → 1-eslatma.
       stage 2 — 1-eslatmadan keyin 3+ kun hamon faol emas → 2-eslatma.
       stage 3 — 30+ kun umumiy faol emas → yangilanishlar eslatmasi.
       stage 4 — 45+ kun → so'nggi eslatma.
       stage 5 — 60+ kun → sukut. Ko'proq eslatma yuborilmaydi.

   Foydalanuvchi biror xabar yoki callback yuborsa — middleware
   `last_active` va `reminder_stage` ni reset qiladi, sikl qaytadan
   boshlanadi.

2. **Router** (`reengagement_router`) — eslatma ostidagi inline tugmalar:
       `rmd_yes` — "👍 Ha, ishlatyapman" — rahmat xabari.
       `rmd_no`  — "😴 Keyinroq ko'raman" — jim tasdiq.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update

from database.engine import AsyncSessionLocal
from database.models import User

logger = logging.getLogger(__name__)

reengagement_router = Router()


# ── Sozlamalar (env orqali qayta sozlash mumkin) ─────────────────

# Birinchi eslatma yuboriladigan jimlik (kun)
STAGE1_INACTIVE_DAYS = int(os.getenv("REENGAGE_STAGE1_DAYS", "21"))
# 1-eslatmadan keyin 2-eslatmagacha oraliq (kun)
STAGE2_GAP_DAYS = int(os.getenv("REENGAGE_STAGE2_GAP_DAYS", "3"))
# 3-bosqich uchun umumiy jimlik (kun)
STAGE3_INACTIVE_DAYS = int(os.getenv("REENGAGE_STAGE3_DAYS", "30"))
# 4-bosqich uchun umumiy jimlik (kun)
STAGE4_INACTIVE_DAYS = int(os.getenv("REENGAGE_STAGE4_DAYS", "45"))
# 5-bosqichdan (sukut) boshlanadigan chegara (kun)
STAGE5_SILENCE_DAYS = int(os.getenv("REENGAGE_SILENCE_DAYS", "60"))
# Scheduler tsikli oralig'i (soat). Standart 24 soat.
LOOP_INTERVAL_HOURS = float(os.getenv("REENGAGE_LOOP_HOURS", "24"))
# Bir tsiklda eng ko'p yuboriladigan xabarlar soni — Telegram rate
# cheklovlariga urilib qolmaslik uchun.
MAX_PER_CYCLE = int(os.getenv("REENGAGE_MAX_PER_CYCLE", "200"))
# Har xabardan keyingi kichik tanaffus (soniya) — rate-friendly.
PER_MESSAGE_DELAY = float(os.getenv("REENGAGE_PER_MSG_DELAY", "0.08"))


def _reminder_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Ha, ishlatyapman", callback_data="rmd_yes", style="success"),
                InlineKeyboardButton(text="😴 Keyinroq ko'raman", callback_data="rmd_no", style="primary"),
            ]
        ]
    )


_STAGE_TEXT = {
    1: (
        "👋 <b>Sog'indik sizni!</b>\n\n"
        "Sizni botda 3 haftadan beri ko'rmadik. Yangi animelar har hafta qo'shilib turadi — "
        "yoqtirgan janringizdan yangiliklarni o'tkazib yubormang.\n\n"
        "Qanday ahvoldasiz?"
    ),
    2: (
        "🎬 <b>Yangi kontentlar qo'shildi</b>\n\n"
        "Oxirgi haftada katalogga yangi epizodlar va filmlar qo'shildi. Kirib ko'ring — "
        "balki sizga yoqadigani chiqqandir.\n\n"
        "Hali ham qiziqasizmi?"
    ),
    3: (
        "✨ <b>Bir oy bo'ldi</b>\n\n"
        "Sizdan ancha xabar yo'q. Agar bot sizga kerak bo'lmasa — hech narsa qilmang, "
        "biz bezovta qilmaymiz. Lekin qaytmoqchi bo'lsangiz — bitta tugma bosing."
    ),
    4: (
        "🙏 <b>So'nggi eslatma</b>\n\n"
        "Bu oxirgi xabarimiz. Quyidagi tugmalardan birini bossangiz — biz bilishimiz uchun, "
        "agar javob bermasangiz — sizga boshqa eslatma yubormaymiz."
    ),
}


async def _send_stage(bot: Bot, user_id: int, stage: int) -> bool:
    """Aniq bosqichdagi eslatmani yuboradi. Muvaffaqiyat bo'lsa True."""
    text = _STAGE_TEXT.get(stage)
    if not text:
        return False
    try:
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=_reminder_kb())
        return True
    except TelegramForbiddenError:
        # User bot'ni blokladi — endi yuborish foydasiz.
        logger.info("reengage: user %s blocked bot, silencing", user_id)
        await _silence_user(user_id)
        return False
    except TelegramRetryAfter as e:
        logger.warning("reengage: rate limited, sleep %.1fs", e.retry_after)
        await asyncio.sleep(e.retry_after + 1)
        return False
    except Exception:
        logger.exception("reengage: send_message failed uid=%s stage=%s", user_id, stage)
        return False


async def _silence_user(user_id: int) -> None:
    """User'ni sukutga o'tkazadi (stage=5) — endi eslatma yubormaymiz."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(User).where(User.telegram_id == user_id).values(reminder_stage=5, last_reminder_at=func.now())
            )
            await session.commit()
    except Exception:
        logger.exception("reengage: silence failed uid=%s", user_id)


async def _mark_sent(user_id: int, stage: int) -> None:
    """Eslatma yuborilganini belgilaydi."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(User)
                .where(User.telegram_id == user_id)
                .values(reminder_stage=stage, last_reminder_at=func.now())
            )
            await session.commit()
    except Exception:
        logger.exception("reengage: mark_sent failed uid=%s stage=%s", user_id, stage)


def _pick_stage(now: datetime, last_active: datetime | None, last_reminder: datetime | None, cur_stage: int) -> int:
    """Foydalanuvchining ahvoliga qarab qaysi bosqich yuborilishini hal qiladi.

    0 qaytsa — hech narsa yubormaymiz (hali erta yoki allaqachon sukut).
    """
    if cur_stage >= 5:
        return 0
    if last_active is None:
        # Ma'lumot yo'q — eslatma yuborishdan saqlanamiz (yangi user).
        return 0
    inactive_days = (now - last_active).days
    if inactive_days < STAGE1_INACTIVE_DAYS:
        return 0
    # 5-bosqich (sukut) chegarasi
    if inactive_days >= STAGE5_SILENCE_DAYS and cur_stage >= 4:
        return 5
    # 4-bosqich
    if inactive_days >= STAGE4_INACTIVE_DAYS and cur_stage < 4:
        return 4
    # 3-bosqich
    if inactive_days >= STAGE3_INACTIVE_DAYS and cur_stage < 3:
        return 3
    # 2-bosqich — 1-eslatmadan keyin belgili muddat o'tgan bo'lsa
    if cur_stage == 1 and last_reminder is not None:
        gap_days = (now - last_reminder).days
        if gap_days >= STAGE2_GAP_DAYS:
            return 2
        return 0
    # 1-bosqich
    if cur_stage == 0:
        return 1
    return 0


async def _run_cycle(bot: Bot) -> int:
    """Bir martalik tsiklni ishga tushiradi. Yuborilgan eslatmalar sonini qaytaradi."""
    sent = 0
    now = datetime.utcnow()
    cutoff = now - timedelta(days=STAGE1_INACTIVE_DAYS)
    try:
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(
                    User.telegram_id,
                    User.last_active,
                    User.last_reminder_at,
                    User.reminder_stage,
                )
                .where(User.last_active.isnot(None))
                .where(User.last_active < cutoff)
                .where((User.reminder_stage.is_(None)) | (User.reminder_stage < 5))
            )
            rows = res.all()
    except Exception:
        logger.exception("reengage: failed to query inactive users")
        return 0

    for row in rows:
        if sent >= MAX_PER_CYCLE:
            logger.info("reengage: cycle reached cap %d, stopping", MAX_PER_CYCLE)
            break
        uid = int(row.telegram_id)
        target = _pick_stage(now, row.last_active, row.last_reminder_at, int(row.reminder_stage or 0))
        if target == 0:
            continue
        if target == 5:
            # Sukutga o'tkazamiz, xabar yubormasdan.
            await _silence_user(uid)
            continue
        ok = await _send_stage(bot, uid, target)
        if ok:
            await _mark_sent(uid, target)
            sent += 1
            await asyncio.sleep(PER_MESSAGE_DELAY)
    logger.info("reengage: cycle complete, sent=%d", sent)
    return sent


async def reengagement_loop(bot: Bot) -> None:
    """Cheksiz loop — har `LOOP_INTERVAL_HOURS` soatda bir marta ishga tushadi."""
    interval = max(1, int(LOOP_INTERVAL_HOURS * 3600))
    logger.info(
        "reengage: scheduler started (stage1=%sd, stage2_gap=%sd, stage3=%sd, stage4=%sd, silence=%sd, interval=%ss)",
        STAGE1_INACTIVE_DAYS,
        STAGE2_GAP_DAYS,
        STAGE3_INACTIVE_DAYS,
        STAGE4_INACTIVE_DAYS,
        STAGE5_SILENCE_DAYS,
        interval,
    )
    # Ishga tushishda darrov ishlamay, bot to'liq ulashishni kutamiz.
    await asyncio.sleep(300)
    while True:
        try:
            await _run_cycle(bot)
        except Exception:
            logger.exception("reengage: cycle crashed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("reengage: scheduler stopped")
            raise


# ── Inline button callbacks ──────────────────────────────────────


@reengagement_router.callback_query(F.data == "rmd_yes")
async def reminder_yes(call: CallbackQuery) -> None:
    """User tasdiqladi — eslatma sonini reset qilamiz (middleware ham qiladi)."""
    try:
        await call.message.edit_text(
            "🔥 <b>Zo'r!</b> Yangi animelar har kuni qo'shilmoqda. Yoqtirgan janringizni ochib, yangiliklarni ko'ring.",
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("rmd_yes: edit_text failed", exc_info=True)
    await call.answer("Rahmat!")


@reengagement_router.callback_query(F.data == "rmd_no")
async def reminder_no(call: CallbackQuery) -> None:
    """User hozir ko'rmoqchi emas — keyingi bosqichgacha jim turamiz."""
    try:
        await call.message.edit_text(
            "😌 Tushunarli. Tayyor bo'lganingizda qayting — biz kutamiz.",
        )
    except Exception:
        logger.debug("rmd_no: edit_text failed", exc_info=True)
    await call.answer()
