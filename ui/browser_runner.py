"""Запуск проверки браузеров из мастера — вне Tkinter, для тестируемости.

Тонкая обвязка вокруг `connectors.browser` (см. CLAUDE.md, раздел
«Not to touch»): сама логика чтения истории и закладок не дублируется,
здесь только последовательный вызов с отчётом о ходе для интерфейса.
"""

from __future__ import annotations

from connectors.browser import BrowserProfile, collect, discover_profiles
from matching.observation import Observation


def run(progress=None, profiles: list[BrowserProfile] | None = None) -> list[Observation]:
    """Проверить все найденные браузеры, сообщая о ходе через ``progress``."""

    def report(text: str) -> None:
        if progress is not None:
            progress(text)

    found = discover_profiles() if profiles is None else profiles
    if not found:
        report(
            "Ни одного браузера не найдено (проверены обычные места "
            "установки Chrome, Edge, Яндекс.Браузера, Opera, Brave, "
            "Vivaldi и Firefox)."
        )
        return []

    report(f"Найдено браузеров/профилей: {len(found)}")
    observations: list[Observation] = []
    for profile in found:
        report(f"Проверяется: {profile.label}")
        observations.extend(collect(profile))
    return observations
