"""Сборка снапшота реестра в файл SQLite.

Запуск::

    python -m registry.build data/spisok.doc -o data/registry.db

Снапшот — самостоятельный файл, который программа проверки просто читает.
Благодаря этому обновление списка не требует пересборки самой программы:
достаточно заменить один файл базы.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .ingest import IngestError, load_html
from .parse import RegistryRow, parse_registry

SCHEMA = """
PRAGMA journal_mode = DELETE;

CREATE TABLE snapshot_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Одна запись = одно судебное решение. Может перечислять несколько
-- «зеркал» одного субъекта в разных соцсетях.
CREATE TABLE entries (
    id            INTEGER PRIMARY KEY,
    row_index     INTEGER NOT NULL,
    material_type TEXT NOT NULL,
    description   TEXT NOT NULL,
    court         TEXT NOT NULL,
    is_cancelled  INTEGER NOT NULL DEFAULT 0,
    is_offline    INTEGER NOT NULL DEFAULT 0
);

-- Опознавательные признаки, по которым идёт сравнение со следами.
CREATE TABLE identifiers (
    id        INTEGER PRIMARY KEY,
    entry_id  INTEGER NOT NULL REFERENCES entries(id),
    platform  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    value     TEXT NOT NULL,
    raw       TEXT NOT NULL,
    notes     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_identifiers_lookup ON identifiers(platform, kind, value);
CREATE INDEX idx_identifiers_value  ON identifiers(value);
CREATE INDEX idx_identifiers_entry  ON identifiers(entry_id);
"""


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_snapshot(rows: list[RegistryRow], destination: Path, source: Path) -> dict:
    """Записать разобранные строки в новый файл базы."""
    if destination.exists():
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(destination)
    try:
        connection.executescript(SCHEMA)

        identifier_count = 0
        for row in rows:
            cursor = connection.execute(
                "INSERT INTO entries (row_index, material_type, description, court,"
                " is_cancelled, is_offline) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row.row_index,
                    row.material_type,
                    row.description,
                    row.court,
                    int(row.is_cancelled),
                    int(row.is_offline_material),
                ),
            )
            entry_id = cursor.lastrowid
            connection.executemany(
                "INSERT INTO identifiers (entry_id, platform, kind, value, raw, notes)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        entry_id,
                        ident.platform,
                        ident.kind,
                        ident.value,
                        ident.raw,
                        "\n".join(ident.notes),
                    )
                    for ident in row.identifiers
                ],
            )
            identifier_count += len(row.identifiers)

        meta = {
            "builder_version": __version__,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_name": source.name,
            "source_sha256": file_digest(source),
            "entry_count": str(len(rows)),
            "identifier_count": str(identifier_count),
        }
        connection.executemany(
            "INSERT INTO snapshot_meta (key, value) VALUES (?, ?)", meta.items()
        )
        connection.commit()
    finally:
        connection.close()

    return {"entries": len(rows), "identifiers": identifier_count}


def summarize(rows: list[RegistryRow]) -> str:
    """Короткий отчёт о том, что удалось разобрать."""
    platforms: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    corrections = 0
    without_identifiers = 0

    for row in rows:
        if row.is_cancelled:
            continue
        if not row.identifiers and not row.is_offline_material:
            without_identifiers += 1
        for ident in row.identifiers:
            platforms[ident.platform] += 1
            kinds[ident.kind] += 1
            if ident.notes:
                corrections += 1

    cancelled = sum(1 for row in rows if row.is_cancelled)
    offline = sum(1 for row in rows if row.is_offline_material)

    lines = [
        f"Записей всего:              {len(rows)}",
        f"  из них отменённых:        {cancelled} (в проверке не участвуют)",
        f"  из них книги/музыка/диски:{offline} (только ручная проверка)",
        f"  без опознанных ссылок:    {without_identifiers}",
        f"Идентификаторов:            {sum(platforms.values())}",
        f"  исправлено написаний:     {corrections}",
        "",
        "По платформам:",
    ]
    for platform, count in platforms.most_common():
        lines.append(f"  {platform:<12} {count}")
    lines.append("")
    lines.append("По типу признака:")
    for kind, count in kinds.most_common():
        lines.append(f"  {kind:<12} {count}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="registry.build",
        description="Собрать снапшот реестра экстремистских материалов в SQLite.",
    )
    parser.add_argument(
        "source",
        type=Path,
        help="исходный файл реестра (.doc с сайта министерства, .docx, .html, .md)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("data/registry.db"),
        help="куда положить снапшот (по умолчанию data/registry.db)",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="только показать статистику, файл базы не создавать",
    )
    args = parser.parse_args(argv)

    try:
        html_text = load_html(args.source)
    except IngestError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    rows = parse_registry(html_text)
    if not rows:
        print(
            "Ошибка: в файле не найдено ни одной строки таблицы. "
            "Возможно, это не тот документ.",
            file=sys.stderr,
        )
        return 3

    print(summarize(rows))

    if not args.stats_only:
        result = write_snapshot(rows, args.output, args.source)
        size_mb = args.output.stat().st_size / (1024 * 1024)
        print(
            f"\nСнапшот записан: {args.output} "
            f"({result['entries']} записей, {size_mb:.1f} МБ)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
