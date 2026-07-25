"""Показ работы движка на выдуманных следах.

Настоящих выгрузок пока нет, поэтому наблюдения берутся синтетические:
несколько записей из реестра плюс заведомо чистые. Это позволяет увидеть,
как выглядит отчёт, и убедиться, что чистые следы не дают совпадений.

Запуск::

    python -m matching.demo data/registry.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .engine import MatchEngine, RegistryIndex
from .observation import observe_handle, observe_numeric_id, observe_url
from .report import render

# Заведомо чистые следы: их в реестре нет, совпадений быть не должно.
CLEAN = [
    observe_handle("telegram", "@my_family_chat", "subscription", "Telegram/диалоги"),
    observe_handle("instagram", "national_geographic", "subscription", "Instagram/подписки"),
    observe_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "view", "YouTube/просмотры"),
    observe_url("https://example.com/article", "visit", "Chrome/история"),
]


def sample_observations(snapshot: Path, limit: int = 4):
    """Сделать «следы» из нескольких настоящих записей реестра."""
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT platform, kind, value FROM identifiers"
            " WHERE kind IN ('handle', 'numeric_id')"
            " GROUP BY platform HAVING COUNT(*) > 10 LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    trace_types = ["subscription", "like", "repost", "view"]
    observations = []
    for index, (platform, kind, value) in enumerate(rows):
        trace = trace_types[index % len(trace_types)]
        source = f"{platform}: выгрузка данных"
        if kind == "numeric_id":
            observations.append(observe_numeric_id(platform, value, trace, source))
        else:
            observations.append(observe_handle(platform, value, trace, source))
    return observations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="matching.demo",
        description="Показать работу сопоставления на выдуманных следах.",
    )
    parser.add_argument("snapshot", type=Path, help="файл базы реестра (registry.db)")
    args = parser.parse_args(argv)

    try:
        index = RegistryIndex(args.snapshot)
    except FileNotFoundError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    observations = sample_observations(args.snapshot)
    for group in CLEAN:
        observations.extend(group if isinstance(group, list) else [group])

    engine = MatchEngine(index)
    matches = engine.match_all(observations)

    print(f"Проверено следов: {len(observations)}")
    print(f"В реестре: {index.entry_count} записей, {index.identifier_count} признаков")
    print()
    print(render(matches, index.meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
