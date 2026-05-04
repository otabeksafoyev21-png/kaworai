"""
Global error handler.

Bot ishlashi davomida tutilmagan istalgan istisno shu yerga tushadi.
Asosiy maqsadlar:
  - Bot "o'lib qolmasin" — istisno tutib olinib, log qilinadi.
  - Admin o'z chatida xabar olsin ("xatolilar kelib chiqsa bot adminga
    xabar bersin").
  - Admin DM spam bo'lib ketmasin — bir xil istisno turi bir xil
    xabar bilan qisqa vaqt ichida faqat bir marta yuboriladi.
  - Foydalanuvchi "qotib qolmasin" — agar mumkin bo'lsa, eventga
    qisqa javob beriladi ("vaqtincha muammo").
"""

from __future__ import annotations

import html
import logging
import time
import traceback
from typing import Any

from aiogram import Router
from aiogram.types import CallbackQuery, ErrorEvent, Message

from data import config

error_router = Router()
logger = logging.getLogger(__name__)

# Admin DM spam oldini olish uchun kichik TTL kesh.
# Key = (istisno turi, xabar birinchi 120 belgisi); qiymat = oxirgi yuborish vaqti.
_ADMIN_NOTIFY_DEDUPE_TTL = 300.0  # 5 daqiqa
_ADMIN_NOTIFY_LAST: dict[tuple[str, str], float] = {}
_ADMIN_NOTIFY_MAX_KEYS = 2048


def _should_notify_admin(exc: BaseException) -> bool:
    """Bir xil xatolar uchun adminga takroriy xabar yuborilmasligiga ishonamiz."""
    now = time.monotonic()
    key = (type(exc).__name__, (str(exc) or "")[:120])
    last = _ADMIN_NOTIFY_LAST.get(key, 0.0)
    if now - last < _ADMIN_NOTIFY_DEDUPE_TTL:
        return False
    _ADMIN_NOTIFY_LAST[key] = now

    # Chekli xotira — eski yozuvlarni tozalash
    if len(_ADMIN_NOTIFY_LAST) > _ADMIN_NOTIFY_MAX_KEYS:
        cutoff = now - _ADMIN_NOTIFY_DEDUPE_TTL
        stale = [k for k, v in _ADMIN_NOTIFY_LAST.items() if v < cutoff]
        for k in stale:
            _ADMIN_NOTIFY_LAST.pop(k, None)
    return True


def _describe_event(event: Any) -> str:
    """Xato chiqqan event haqida qisqa, PII'siz tasvir."""
    update = getattr(event, "update", None)
    if update is None:
        return "—"
    msg = getattr(update, "message", None)
    if msg is not None:
        user_id = getattr(getattr(msg, "from_user", None), "id", None)
        text = (msg.text or msg.caption or "")[:80]
        return f"message user_id={user_id} text={text!r}"
    cb = getattr(update, "callback_query", None)
    if cb is not None:
        user_id = getattr(getattr(cb, "from_user", None), "id", None)
        return f"callback user_id={user_id} data={(cb.data or '')[:80]!r}"
    inline = getattr(update, "inline_query", None)
    if inline is not None:
        user_id = getattr(getattr(inline, "from_user", None), "id", None)
        return f"inline user_id={user_id} q={(inline.query or '')[:60]!r}"
    return f"update_id={getattr(update, 'update_id', '?')}"


async def _safe_reply_to_user(event: ErrorEvent) -> None:
    """Foydalanuvchiga — agar mumkin bo'lsa — qisqa xabar qaytaramiz."""
    update = getattr(event, "update", None)
    if update is None:
        return
    try:
        msg = getattr(update, "message", None)
        if isinstance(msg, Message):
            await msg.answer("⚠️ Kechirasiz, ichki xato. Admin xabardor qilindi.")
            return
        cb = getattr(update, "callback_query", None)
        if isinstance(cb, CallbackQuery):
            await cb.answer("⚠️ Xato. Admin xabardor qilindi.", show_alert=False)
    except Exception:
        # Shu yerda ham yiqilmasligimiz kerak — unutilmasin, biz error
        # handler ichidamiz. Tashqariga exception chiqarib yuborish
        # aiogram'ning o'z loopini portlatishi mumkin.
        pass


async def _notify_admin(event: ErrorEvent) -> None:
    exc = event.exception
    if not _should_notify_admin(exc):
        return

    admin_id = getattr(config, "ADMIN_ID", 0) or 0
    if not admin_id:
        return

    try:
        from loader import bot
    except Exception:
        return

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    tb_tail = tb[-1500:]  # Telegram xabarlari 4096 belgidan ko'p bo'la olmaydi
    where = _describe_event(event)
    # Tracebacklar odatda `<module>`, `<stdin>`, `list[dict[...]]` kabi HTML
    # ko'rinishida yozilgan matnlarni o'z ichiga oladi. Escape qilmasak,
    # Telegram parserga noto'g'ri tag ko'rinib send_message xato beradi va
    # admin xabarni umuman olmaydi.
    text = (
        "🚨 <b>Bot xatosi</b>\n"
        f"<code>{html.escape(type(exc).__name__)}</code>: "
        f"{html.escape(str(exc)[:300])}\n\n"
        f"<i>Event:</i> <code>{html.escape(where[:200])}</code>\n\n"
        f"<pre>{html.escape(tb_tail)}</pre>"
    )
    try:
        await bot.send_message(admin_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("errors: failed to notify admin_id=%s", admin_id)


@error_router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """
    Barcha tutilmagan istisnolarni ushlaydigan global handler.

    True qaytarish — istisno aiogram tomonidan "hal qilingan" deb
    hisoblanadi, ya'ni u yana qayta raise qilinmaydi va bot yiqilmaydi.
    """
    exc = event.exception
    where = _describe_event(event)
    logger.exception("unhandled exception at %s: %s", where, exc)

    await _safe_reply_to_user(event)
    await _notify_admin(event)
    return True
