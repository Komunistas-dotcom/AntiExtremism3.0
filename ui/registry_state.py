"""Загрузка и сборка базы реестра — вне Tkinter, чтобы это было тестируемо.

Обёртка вокруг `registry.build`/`registry.ingest` (см. CLAUDE.md: эти модули
в разделе «Not to touch» — здесь только вызываются их готовые функции,
собственная логика разбора не дублируется и не меняется).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from registry.build import write_snapshot
from registry.ingest import IngestError, load_html
from registry.parse import parse_registry

REPO_ROOT = Path(__file__).resolve().parent.parent

# Форматы, из которых можно собрать базу — то же самое, что понимает
# registry.ingest.load_html, плюс уже готовый .db.
SOURCE_SUFFIXES = (".doc", ".docx", ".odt", ".rtf", ".html", ".htm", ".md", ".markdown")


def default_registry_path(root: Path | None = None) -> Path:
    """Где мастер ищет готовую базу по умолчанию.

    Параметр ``root`` нужен только тестам — по умолчанию это корень репозитория.
    """
    return (root or REPO_ROOT) / "data" / "registry.db"


def find_default_source(root: Path | None = None) -> Path | None:
    """Найти исходный файл реестра в data/, если готовой базы ещё нет."""
    data_dir = (root or REPO_ROOT) / "data"
    if not data_dir.is_dir():
        return None
    for suffix in SOURCE_SUFFIXES:
        matches = sorted(data_dir.glob(f"*{suffix}"))
        if matches:
            return matches[0]
    return None


@dataclass
class RegistrySummary:
    """Короткое описание загруженной базы для первого экрана мастера."""

    path: Path
    entry_count: int
    identifier_count: int
    built_at: str
    age_days: int | None

    @property
    def age_label(self) -> str:
        if self.age_days is None:
            return ""
        if self.age_days == 0:
            return "собрана сегодня"
        if self.age_days == 1:
            return "собрана вчера"
        return f"собрана {self.age_days} дн. назад"


def summarize_meta(path: Path, meta: dict[str, str]) -> RegistrySummary:
    """Превратить сырые метаданные снапшота в удобный для показа вид."""
    built_at_raw = meta.get("built_at", "")
    age_days = None
    built_at_label = built_at_raw
    try:
        built_at = datetime.fromisoformat(built_at_raw)
        age_days = (datetime.now(timezone.utc) - built_at).days
        built_at_label = built_at.strftime("%d.%m.%Y")
    except ValueError:
        pass

    return RegistrySummary(
        path=path,
        entry_count=int(meta.get("entry_count", 0) or 0),
        identifier_count=int(meta.get("identifier_count", 0) or 0),
        built_at=built_at_label,
        age_days=age_days,
    )


def build_snapshot(
    source: Path, destination: Path, progress=None
) -> dict:
    """Собрать снапшот из исходного файла реестра.

    ``progress``, если передан, вызывается с короткими текстовыми
    сообщениями о ходе сборки — для показа в интерфейсе.
    """

    def report(text: str) -> None:
        if progress is not None:
            progress(text)

    report(f"Читается {source.name}…")
    try:
        html_text = load_html(source)
    except IngestError as exc:
        raise RuntimeError(str(exc)) from exc

    report("Разбирается таблица реестра…")
    rows = parse_registry(html_text)
    if not rows:
        raise RuntimeError(
            "В файле не найдено ни одной строки таблицы. "
            "Похоже, это не тот документ."
        )

    report(f"Записывается база ({len(rows)} записей)…")
    result = write_snapshot(rows, destination, source)
    report("База реестра собрана.")
    return result
