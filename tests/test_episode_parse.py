"""Episode caption -> raqam aniqlash regexlari uchun test.

Bot bulk-upload'da videolar caption'idan qism raqamini ajratib oladi.
Regex'lar ko'p (uz/en/ru/hashtag) — har formatga 1-2 ta sanity test.
"""

from __future__ import annotations

import pytest

from handlers.admin import _detect_episode_from_caption


@pytest.mark.parametrize(
    "caption,expected",
    [
        ("66 - Qism", 66),
        ("66-qism", 66),
        ("66 qism", 66),
        ("Qism: 66", 66),
        ("Qism #66", 66),
        ("66-seriya", 66),
        ("Seriya 66", 66),
        ("66-epizod", 66),
        ("Epizot 66", 66),
        ("66 - part", 66),
        ("Episode 66", 66),
        ("Ep. 66", 66),
        ("S01E66", 66),
        ("66 серия", 66),
        ("Серия 66", 66),
        ("Эпизод 66", 66),
        ("#66", 66),
        ("№ 66", 66),
    ],
)
def test_episode_parsed(caption: str, expected: int) -> None:
    assert _detect_episode_from_caption(caption) == expected, caption


@pytest.mark.parametrize(
    "caption",
    [
        "",
        "Qandaydir matn raqamsiz",
        "Title without numbers",
    ],
)
def test_episode_not_parsed(caption: str) -> None:
    assert _detect_episode_from_caption(caption) is None, caption


def test_episode_none_input() -> None:
    assert _detect_episode_from_caption(None) is None  # type: ignore[arg-type]
