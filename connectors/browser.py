"""История и закладки браузеров.

Этот модуль ценен тем, что **не требует ждать выгрузку данных**: история
лежит на диске и читается сразу. Остальные источники (Meta, TikTok) готовятся
от нескольких часов до нескольких суток, поэтому браузер даёт человеку
первый результат в первую же минуту.

Проверяется по перечню сайтов из реестра, а заодно ловит и ссылки на
соцсети — в истории они встречаются в исходном виде.

О приватности
-------------
Браузер держит свою базу открытой, поэтому читать её напрямую нельзя —
делается временная копия. Копия удаляется всегда, даже при ошибке. Никакие
данные истории на диск не записываются: наблюдения отдаются потоком и живут
только в памяти.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from matching.observation import Observation, observe_visited_domain

# Chromium считает время в микросекундах от 1 января 1601 года.
_CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

CHROMIUM = "chromium"
FIREFOX = "firefox"


@dataclass(frozen=True)
class BrowserProfile:
    """Один профиль одного браузера."""

    browser: str
    profile: str
    engine: str
    history: Path
    bookmarks: Path | None = None

    @property
    def label(self) -> str:
        return f"{self.browser} ({self.profile})" if self.profile else self.browser


# Где какой браузер хранит данные. Windows — основная система для программы,
# пути для Linux и macOS нужны для разработки и проверки.
def _chromium_roots() -> dict[str, list[Path]]:
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        roaming = Path(os.environ.get("APPDATA", ""))
        return {
            "Chrome": [local / "Google/Chrome/User Data"],
            "Edge": [local / "Microsoft/Edge/User Data"],
            "Яндекс.Браузер": [local / "Yandex/YandexBrowser/User Data"],
            "Opera": [roaming / "Opera Software/Opera Stable"],
            "Brave": [local / "BraveSoftware/Brave-Browser/User Data"],
            "Vivaldi": [local / "Vivaldi/User Data"],
        }
    home = Path.home()
    if sys.platform == "darwin":
        support = home / "Library/Application Support"
        return {
            "Chrome": [support / "Google/Chrome"],
            "Edge": [support / "Microsoft Edge"],
            "Яндекс.Браузер": [support / "Yandex/YandexBrowser"],
            "Brave": [support / "BraveSoftware/Brave-Browser"],
        }
    config = home / ".config"
    return {
        "Chrome": [config / "google-chrome", config / "chromium"],
        "Edge": [config / "microsoft-edge"],
        "Яндекс.Браузер": [config / "yandex-browser"],
        "Brave": [config / "BraveSoftware/Brave-Browser"],
    }


def _firefox_roots() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        return [Path(os.environ.get("APPDATA", "")) / "Mozilla/Firefox/Profiles"]
    if sys.platform == "darwin":
        return [home / "Library/Application Support/Firefox/Profiles"]
    return [home / ".mozilla/firefox"]


def discover_profiles(
    chromium_roots: dict[str, list[Path]] | None = None,
    firefox_roots: list[Path] | None = None,
) -> list[BrowserProfile]:
    """Найти установленные браузеры и их профили.

    У одного браузера профилей бывает несколько («Default», «Profile 1»),
    и проверять надо каждый: человек мог смотреть ресурсы под любым из них.
    """
    profiles: list[BrowserProfile] = []

    for browser, roots in (chromium_roots or _chromium_roots()).items():
        for root in roots:
            if not root.is_dir():
                continue
            # История лежит либо прямо в корне (Opera), либо в подпапках профилей.
            candidates = [root, *sorted(p for p in root.iterdir() if p.is_dir())]
            for directory in candidates:
                history = directory / "History"
                if not history.is_file():
                    continue
                bookmarks = directory / "Bookmarks"
                profiles.append(
                    BrowserProfile(
                        browser=browser,
                        profile="" if directory == root else directory.name,
                        engine=CHROMIUM,
                        history=history,
                        bookmarks=bookmarks if bookmarks.is_file() else None,
                    )
                )

    for root in firefox_roots or _firefox_roots():
        if not root.is_dir():
            continue
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            places = directory / "places.sqlite"
            if places.is_file():
                profiles.append(
                    BrowserProfile(
                        browser="Firefox",
                        profile=directory.name,
                        engine=FIREFOX,
                        history=places,
                    )
                )

    return profiles


class _TemporaryCopy:
    """Копия базы браузера на время чтения.

    Браузер держит свой файл открытым, поэтому читаем копию. Копия
    удаляется в любом случае — в том числе если чтение прервалось ошибкой.
    """

    def __init__(self, source: Path) -> None:
        self._source = source
        self._directory: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> Path:
        self._directory = tempfile.TemporaryDirectory(prefix="browser-read-")
        target = Path(self._directory.name) / self._source.name
        shutil.copy2(self._source, target)
        # Незаписанные страницы лежат в спутниках файла: без них часть
        # свежей истории не увидеть.
        for suffix in ("-wal", "-shm"):
            companion = self._source.with_name(self._source.name + suffix)
            if companion.is_file():
                shutil.copy2(companion, target.with_name(target.name + suffix))
        return target

    def __exit__(self, *exc_info) -> None:
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


def _chromium_time(value: int | None) -> str | None:
    if not value:
        return None
    try:
        return (_CHROMIUM_EPOCH + timedelta(microseconds=value)).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _unix_time(value: int | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value / 1_000_000, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _query(database: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return connection.execute(sql).fetchall()
    except sqlite3.DatabaseError:
        # Повреждённый или непривычный формат — пропускаем этот профиль,
        # остальные проверить всё равно нужно.
        return []
    finally:
        connection.close()


def read_history(profile: BrowserProfile) -> list[tuple[str, str | None, int]]:
    """Прочитать историю профиля: адрес, дата последнего посещения, счётчик."""
    with _TemporaryCopy(profile.history) as copy:
        if profile.engine == CHROMIUM:
            rows = _query(
                copy, "SELECT url, last_visit_time, visit_count FROM urls"
            )
            return [(url, _chromium_time(time), count or 1) for url, time, count in rows]

        rows = _query(
            copy,
            "SELECT url, last_visit_date, visit_count FROM moz_places"
            " WHERE url IS NOT NULL",
        )
        return [(url, _unix_time(time), count or 1) for url, time, count in rows]


def _walk_chromium_bookmarks(node, found: list[str]) -> None:
    """Обойти дерево закладок.

    Закладки вложены произвольно глубоко, а на верхнем уровне лежат не
    «дети», а именованные разделы («панель закладок», «прочие»), поэтому
    обход спускается и в значения словаря.
    """
    if isinstance(node, dict):
        if node.get("type") == "url" and node.get("url"):
            found.append(node["url"])
            return
        children = node.get("children")
        for child in children if children is not None else node.values():
            _walk_chromium_bookmarks(child, found)
    elif isinstance(node, list):
        for child in node:
            _walk_chromium_bookmarks(child, found)


def read_bookmarks(profile: BrowserProfile) -> list[str]:
    """Прочитать закладки профиля."""
    if profile.engine == CHROMIUM:
        if not profile.bookmarks:
            return []
        try:
            data = json.loads(profile.bookmarks.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found: list[str] = []
        _walk_chromium_bookmarks(data.get("roots", {}), found)
        return found

    with _TemporaryCopy(profile.history) as copy:
        rows = _query(
            copy,
            "SELECT p.url FROM moz_bookmarks b JOIN moz_places p ON p.id = b.fk"
            " WHERE b.type = 1 AND p.url IS NOT NULL",
        )
    return [row[0] for row in rows]


def collect(profile: BrowserProfile) -> Iterator[Observation]:
    """Наблюдения из одного профиля браузера.

    Одинаковые адреса схлопываются: сайт из истории обычно встречается
    десятки раз, и показывать его в отчёте десятки раз бессмысленно.
    Число посещений при этом сохраняется — оно помогает человеку понять,
    зашёл он один раз случайно или читал регулярно.
    """
    totals: dict[str, int] = {}
    latest: dict[str, str | None] = {}
    sample: dict[str, Observation] = {}

    for url, occurred_at, count in read_history(profile):
        for observation in observe_visited_domain(url, profile.label, occurred_at):
            key = observation.key
            totals[key] = totals.get(key, 0) + count
            if occurred_at and (latest.get(key) is None or occurred_at > latest[key]):
                latest[key] = occurred_at
            sample.setdefault(key, observation)

    for key, observation in sample.items():
        visits = totals[key]
        yield Observation(
            platform=observation.platform,
            kind=observation.kind,
            value=observation.value,
            trace_type="visit",
            source=f"{profile.label}, история — посещений: {visits}",
            raw=observation.raw,
            occurred_at=latest.get(key),
        )

    seen_bookmarks: set[str] = set()
    for url in read_bookmarks(profile):
        for observation in observe_visited_domain(url, profile.label):
            if observation.key in seen_bookmarks:
                continue
            seen_bookmarks.add(observation.key)
            yield Observation(
                platform=observation.platform,
                kind=observation.kind,
                value=observation.value,
                trace_type="saved",
                source=f"{profile.label}, закладки",
                raw=observation.raw,
            )


def collect_all(profiles: list[BrowserProfile] | None = None) -> Iterator[Observation]:
    """Наблюдения из всех найденных браузеров."""
    for profile in profiles if profiles is not None else discover_profiles():
        yield from collect(profile)
