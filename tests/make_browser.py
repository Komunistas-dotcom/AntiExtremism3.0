"""Создание настоящих файлов баз браузеров для тестов."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def chromium_time(iso_date: str) -> int:
    moment = datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
    return int((moment - CHROMIUM_EPOCH).total_seconds() * 1_000_000)


def unix_time(iso_date: str) -> int:
    moment = datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1_000_000)


def build_chromium_profile(
    directory: Path,
    history: list[tuple[str, str, int]],
    bookmarks: list[str] | None = None,
) -> Path:
    """Профиль браузера на движке Chromium: файлы History и Bookmarks."""
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "History")
    try:
        connection.execute(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, url LONGVARCHAR,"
            " title LONGVARCHAR, visit_count INTEGER, typed_count INTEGER,"
            " last_visit_time INTEGER, hidden INTEGER)"
        )
        connection.executemany(
            "INSERT INTO urls (url, title, visit_count, typed_count,"
            " last_visit_time, hidden) VALUES (?, ?, ?, 0, ?, 0)",
            [
                (url, "заголовок", count, chromium_time(date))
                for url, date, count in history
            ],
        )
        connection.commit()
    finally:
        connection.close()

    if bookmarks is not None:
        payload = {
            "roots": {
                "bookmark_bar": {
                    "type": "folder",
                    "children": [
                        {
                            "type": "folder",
                            "name": "папка",
                            "children": [
                                {"type": "url", "name": "закладка", "url": url}
                                for url in bookmarks
                            ],
                        }
                    ],
                },
                "other": {"type": "folder", "children": []},
            }
        }
        (directory / "Bookmarks").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    return directory


def build_firefox_profile(
    directory: Path,
    history: list[tuple[str, str, int]],
    bookmarks: list[str] | None = None,
) -> Path:
    """Профиль Firefox: файл places.sqlite."""
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "places.sqlite")
    try:
        connection.execute(
            "CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url LONGVARCHAR,"
            " title LONGVARCHAR, visit_count INTEGER, last_visit_date INTEGER)"
        )
        connection.execute(
            "CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, type INTEGER,"
            " fk INTEGER, title LONGVARCHAR)"
        )
        for index, (url, date, count) in enumerate(history, start=1):
            connection.execute(
                "INSERT INTO moz_places (id, url, title, visit_count,"
                " last_visit_date) VALUES (?, ?, ?, ?, ?)",
                (index, url, "заголовок", count, unix_time(date)),
            )
        for index, url in enumerate(bookmarks or [], start=1):
            connection.execute(
                "INSERT INTO moz_places (id, url, title, visit_count,"
                " last_visit_date) VALUES (?, ?, ?, 1, NULL)",
                (1000 + index, url, "закладка"),
            )
            connection.execute(
                "INSERT INTO moz_bookmarks (type, fk, title) VALUES (1, ?, ?)",
                (1000 + index, "закладка"),
            )
        connection.commit()
    finally:
        connection.close()
    return directory
