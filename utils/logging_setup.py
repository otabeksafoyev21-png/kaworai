"""
Markaziy logging sozlamasi — matn yoki JSON formatida.

Nega kerak:
  - Railway log'larida oddiy matn bo'lsa, filter qilish qiyin: "qaysi
    user_id 500ta xato bergan?" degan savolga javob berish uchun katta
    log'ni qo'lda kovlab chiqasiz.
  - JSON format bilan Railway (va keyin Datadog/Loki/ELK) yozuvlarni
    `user_id`, `anime_id`, `level` bo'yicha darhol filterlaydi.
  - Odatiy bot.log fayli ham ishlayveradi — lokal dev uchun qulay.

Ishlatish:
    from utils.logging_setup import setup_logging
    setup_logging()                 # LOG_FORMAT env'idan oladi
    logger.info("...", extra={"user_id": uid})

ENV var'lari:
  - LOG_FORMAT=json | text  — default 'text'. Railway prod'da 'json'.
  - LOG_LEVEL=INFO | DEBUG | ...  — default 'INFO'.
  - LOG_FILE=bot.log        — fayl nomi; bo'sh qolsa fayl chiqarilmaydi.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

# `extra={...}` orqali uzatilgan custom maydonlar. LogRecord'ning standart
# atributlari bilan aralashib ketmasligi uchun JSON formatter shu ro'yxatni
# bilishi kerak (qolgan hamma narsa structured field deb sanaladi).
_LOGRECORD_STANDARD_ATTRS = frozenset(
    (
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    )
)


class JsonFormatter(logging.Formatter):
    """
    Log record'ni JSON obyektga aylantiradi. Har bir qator — bitta JSON.

    Stantsart maydonlar: ts, level, logger, message. Tracebacklar
    `exc_info` maydoniga matn shaklida qo'yiladi. `extra={...}` orqali
    uzatilgan qo'shimcha field'lar yuqori darajaga ko'chiriladi
    (masalan user_id, anime_id).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info

        # `extra={...}` dan kelgan maydonlar — LogRecord'da boshqa nomlar bilan
        # bor, shuning uchun standart bo'lmaganlarni oldik.
        for key, value in record.__dict__.items():
            if key in _LOGRECORD_STANDARD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    """
    Root logger'ni sozlaydi. `bot.py:main()` bir marta chaqiradi.

    LOG_FORMAT=json  → JsonFormatter, stdout'ga.
    LOG_FORMAT=text  → odatiy matn formati (eski ko'rinish).

    Qayta chaqirilsa — eski handler'lar olib tashlanadi (idempotent).
    """
    fmt = (os.getenv("LOG_FORMAT", "text") or "text").strip().lower()
    level_name = (os.getenv("LOG_LEVEL", "INFO") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = (os.getenv("LOG_FILE", "bot.log") or "").strip()

    root = logging.getLogger()
    # Eski basicConfig handler'larini olib tashlaymiz, aks holda xabar
    # ikki marta chiqadi (bir text, bir JSON).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    if fmt == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            # Read-only filesystem'da (masalan ba'zi konteynerlar) — o'tkazib yuboramiz
            logging.getLogger(__name__).warning("logging: %s yozib bo'lmadi, stdout'da davom", log_file)

    logging.getLogger(__name__).info("logging: format=%s level=%s file=%s", fmt, level_name, log_file or "(off)")
