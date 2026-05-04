"""Filler caption aniqlash regexlari uchun test.

Maqsad — admin bulk-upload paytida `[FILLER]`, `(filler)`, `ФИЛЛЕР` va
`to'ldiruvchi` kabi belgilarni xato qoldirib ketmasligimizga ishonch hosil
qilish. Regex murakkab — har lang/format'ga alohida tayanch test.
"""

from __future__ import annotations

import pytest

from handlers.admin import _detect_filler_from_caption


@pytest.mark.parametrize(
    "caption",
    [
        "66 - Qism [FILLER]",
        "66-Qism (FILLER)",
        "66 qism FILLER",
        "Episode 66 — filler",
        "S01E66 Filler",
        "66-серия ФИЛЛЕР",
        "66 серия (филлер)",
        "66 - qism toldiruvchi",
        "66 - qism to'ldiruvchi",
        "66 - qism to_ldiruvchi",
    ],
)
def test_filler_detected(caption: str) -> None:
    assert _detect_filler_from_caption(caption) is True, caption


@pytest.mark.parametrize(
    "caption",
    [
        "",
        "66 - Qism",
        "Episode 66",
        "S01E66",
        "Filling station",  # 'fill' lekin 'filler' emas
        "Fillet of fish",
        "66 серия",
    ],
)
def test_filler_not_detected(caption: str) -> None:
    assert _detect_filler_from_caption(caption) is False, caption


def test_filler_none_input() -> None:
    # type: ignore[arg-type]  — defensiv: caption=None bo'lsa False qaytarsin.
    assert _detect_filler_from_caption(None) is False  # type: ignore[arg-type]
