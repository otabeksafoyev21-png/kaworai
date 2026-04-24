"""
Broadcast yordamchilari — Telegram flood-wait va forbidden xatolarini
to'g'ri ushlash uchun.

Nega alohida modul:
  - admin.py'da bir nechta joyda user'larga xabar yuboriladi: broadcast,
    anime post, kanallarga post, region so'rash, va boshqalar.
  - Oldin ular hammasi `except Exception: failed += 1` bilan ishlardi —
    bu `TelegramRetryAfter` (flood wait) ni xato deb sanardi, holbuki
    uni kutsak, 99% xabarlar yetib boradi.
  - 200k userga broadcast qilganda, Telegram sekundiga ~30 xabar
    chegara qo'yadi. `TelegramRetryAfter.retry_after` qiymatini kutish
    kerak, keyin qayta urinib ko'rish kerak.

Ishlatish:
    from utils.broadcast import send_with_retry
    success, failed, blocked = 0, 0, 0
    for uid in user_ids:
        result = await send_with_retry(lambda: bot.send_message(uid, text))
        if result == "ok":
            success += 1
        elif result == "blocked":
            blocked += 1
        else:
            failed += 1
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Literal

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)

logger = logging.getLogger(__name__)

SendResult = Literal["ok", "blocked", "failed"]

# Broadcast davomida Telegram "Too Many Requests" qaytarsa, shuncha marta
# qayta urinib ko'ramiz. Keyin voz kechamiz — aks holda bitta user bot'ni
# cheksiz bloklashi mumkin.
_MAX_FLOOD_RETRIES = 3

# Eng uzun flood-wait intervali. Telegram ba'zida 60s+ so'raydi —
# bu 200k user broadcast'ida ham qabul qilsak bo'ladi, lekin undan
# ko'prog'ini kutmaymiz (orchestrator health-check buzilishi mumkin).
_MAX_RETRY_AFTER_SEC = 300


async def send_with_retry(send_fn: Callable[[], Awaitable[object]]) -> SendResult:
    """
    `send_fn()` ni chaqiradi va Telegram xatolarini to'g'ri ushlaydi.

    - TelegramRetryAfter: `retry_after` soniyalarini kutib, qayta urinadi
      (maksimum _MAX_FLOOD_RETRIES marta).
    - TelegramForbiddenError: user bot'ni bloklagan — 'blocked' qaytaradi.
    - TelegramNotFound: user/chat topilmadi — 'blocked' deb hisoblaymiz
      (qayta yuborish behuda, faqat statistika uchun).
    - TelegramBadRequest (user deactivated/chat not found kabi): 'blocked'.
    - Boshqa xato: 'failed', log qilinadi.
    """
    for attempt in range(_MAX_FLOOD_RETRIES):
        try:
            await send_fn()
            return "ok"
        except TelegramRetryAfter as e:
            retry_after = min(int(e.retry_after) + 1, _MAX_RETRY_AFTER_SEC)
            if attempt + 1 >= _MAX_FLOOD_RETRIES:
                logger.warning(
                    "broadcast: flood-wait %ds, retry limit (%d) tugadi",
                    retry_after,
                    _MAX_FLOOD_RETRIES,
                )
                return "failed"
            logger.info(
                "broadcast: flood-wait %ds, kutilmoqda (attempt %d/%d)",
                retry_after,
                attempt + 1,
                _MAX_FLOOD_RETRIES,
            )
            await asyncio.sleep(retry_after)
            continue
        except TelegramForbiddenError:
            # User bot'ni bloklagan yoki o'chirilgan — qayta yuborish shart emas.
            return "blocked"
        except TelegramNotFound:
            # Chat yo'q (user deleted/not started) — blocked deb hisoblaymiz.
            return "blocked"
        except TelegramBadRequest as e:
            msg = str(e).lower()
            # Telegram bu holatlarni ko'pincha TelegramBadRequest shaklida qaytaradi.
            if any(
                hint in msg
                for hint in (
                    "chat not found",
                    "user is deactivated",
                    "bot was blocked",
                    "peer_id_invalid",
                )
            ):
                return "blocked"
            logger.warning("broadcast: BadRequest: %s", e)
            return "failed"
        except Exception:
            logger.exception("broadcast: kutilmagan xato")
            return "failed"

    return "failed"
