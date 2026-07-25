"""Проверка истории браузеров по реестру.

Запуск::

    python -m connectors.check_browser data/registry.db

Отчёт по умолчанию только показывается на экране. Чтобы сохранить его в
файл, нужно явно указать ``--save``: перечень собственных следов на диске
— это, по сути, готовая улика, и создавать её незаметно нельзя.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from matching.engine import MatchEngine, RegistryIndex
from matching.report import WARNING, render, save

from .browser import collect_all, discover_profiles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="connectors.check_browser",
        description="Проверить историю и закладки браузеров по реестру.",
    )
    parser.add_argument("snapshot", type=Path, help="файл базы реестра (registry.db)")
    parser.add_argument(
        "--save",
        type=Path,
        metavar="ФАЙЛ",
        help="сохранить отчёт в файл (по умолчанию отчёт никуда не пишется)",
    )
    args = parser.parse_args(argv)

    try:
        index = RegistryIndex(args.snapshot)
    except FileNotFoundError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    profiles = discover_profiles()
    if not profiles:
        print(
            "Ни одного браузера не найдено.\n"
            "Проверены обычные места установки Chrome, Edge, Яндекс.Браузера, "
            "Opera, Brave, Vivaldi и Firefox."
        )
        return 1

    print("Проверяются:")
    for profile in profiles:
        print(f"  • {profile.label}")
    print()

    engine = MatchEngine(index)
    matches = engine.match_all(collect_all(profiles))
    report = render(matches, index.meta)
    print(report)

    if args.save:
        print()
        print(WARNING)
        path = save(report, args.save)
        print(f"Отчёт сохранён: {path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
