"""
Kechqurun uxlash eslatmasi — 22:00 dan keyin 3+ qism ko'rgan userlarga
yumshoq eslatma ko'rsatadi.

In-memory counter — bot restart bo'lsa noldan boshlanadi (muhim emas,
chunki counter faqat bitta sessiya davomida ishlaydi).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

# user_id → {"count": int, "window_start": float}
_SESSION_COUNTERS: dict[int, dict[str, int | float]] = {}

# Bir kechada faqat bitta alert ko'rsatilsin
_ALERTED_TONIGHT: set[int] = set()

# 3 soatlik oyna — 3 soat ichida 3+ qism ko'rsa eslatma chiqadi
_WINDOW_SEC = 3 * 60 * 60

# O'zbekiston UTC+5
_UZB_UTC_OFFSET = 5

SLEEP_MESSAGES = [
    "🌙 Assalomu alaykum! Soat kech bo'lib qoldi — uxlashni ham esingizdan chiqarmang! 😴",
    "🌙 3 ta qism ko'rdingiz, zo'r! Lekin sog'liq uchun dam olish ham muhim. Uxlashni unutmang! 💤",
    "🌙 Kaworai sizni yaxshi ko'radi, lekin uxlash ham kerak! Ertaga davom etamiz 😊💤",
    "🌙 Siz ajoyib tomoshabin ekansiz! Endi esa — yaxshi uxlang, ertaga yangi qismlar kutadi! 🌟",
]


def _uzb_hour() -> int:
    """O'zbekiston vaqti bo'yicha hozirgi soat."""
    utc_now = datetime.now(tz=timezone.utc)
    return (utc_now.hour + _UZB_UTC_OFFSET) % 24


def _is_late_night() -> bool:
    """22:00 dan 05:00 gacha — kech soatlar."""
    h = _uzb_hour()
    return h >= 22 or h < 5


def record_episode_view(user_id: int) -> str | None:
    """Qism ko'rilganini qayd etadi.

    Agar 22:00+ va 3+ qism ko'rilgan bo'lsa — eslatma matnini qaytaradi.
    Aks holda None.
    """
    now = time.monotonic()

    data = _SESSION_COUNTERS.get(user_id)
    if data is None or (now - data["window_start"]) > _WINDOW_SEC:
        _SESSION_COUNTERS[user_id] = {"count": 1, "window_start": now}
        return None

    data["count"] = int(data["count"]) + 1
    count = int(data["count"])

    if count >= 3 and _is_late_night() and user_id not in _ALERTED_TONIGHT:
        _ALERTED_TONIGHT.add(user_id)
        import random

        return random.choice(SLEEP_MESSAGES)

    return None


def reset_nightly_alerts() -> None:
    """Har kuni ertalab chaqiriladi — kechagi alertlarni tozalaydi."""
    _ALERTED_TONIGHT.clear()
