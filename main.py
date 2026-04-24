"""
Railway va ba'zi deploy toollari odatda `main.py` ni entrypoint sifatida
oladi (nixpacks default: `python main.py`). Loyiha haqiqiy entrypoint'i
esa `bot.py` — u MemoryStorage FSM fallback, pro_payment, admin_pro,
pro_user, inline va genre routerlari, global error_router, hamda admin
tugma outer middleware'ni o'z ichiga oladi.

Eski `main.py` to'g'ridan-to'g'ri `RedisStorage.from_url(...)` ishlatardi
va faqat 4 ta router'ni include qilardi. Natijada Railway'da Redis
javobsiz qolgan paytda admin tugmalari "jim" bo'lib, pro to'lov va admin
pro menyular umuman ishga tushmasdi — chunki Railway aynan shu eski
`main.py` ni ishlatib kelgan. Loglarda `INFO:root:Bot ishga tushdi`
qatori shu `main.py` dan chiqardi (yangi `bot.py` esa
`--- BOT INSTANCE pid=... ISHGA TUSHDI ---` yozadi).

Bu fayl endi faqat `bot.py:main` ni chaqiradi — shunday qilib qaysi
entrypoint tanlansa ham (main.py yoki bot.py) bitta zamonaviy kod yo'li
ishlaydi.
"""

import asyncio
import logging

from bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot foydalanuvchi tomonidan to'xtatildi.")
