"""Отчёт о найденных совпадениях.

Отчёт по умолчанию **никуда не сохраняется** — он собирается в память и
показывается на экране. Сохранение в файл возможно только явным действием
пользователя: готовый список собственных следов на диске это, по сути,
готовая улика, и создавать её незаметно для человека нельзя.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .engine import EXACT, PROBABLE, Match

WARNING = (
    "Этот отчёт содержит перечень ваших собственных следов. "
    "Сохраняйте его только если понимаете, зачем, и удалите, когда он "
    "перестанет быть нужен."
)


def _shorten(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize(matches: list[Match]) -> dict[str, int]:
    exact = sum(1 for m in matches if m.confidence == EXACT)
    return {
        "всего": len(matches),
        "точных": exact,
        "вероятных": len(matches) - exact,
        "записей реестра": len({m.entry.entry_id for m in matches}),
    }


def render(matches: list[Match], index_meta: dict[str, str] | None = None) -> str:
    """Собрать текст отчёта.

    Совпадения сгруппированы по записи реестра: одно судебное решение
    часто перечисляет несколько «зеркал», и человеку удобнее видеть их
    вместе, а не по отдельности.
    """
    lines: list[str] = ["ОТЧЁТ О САМОПРОВЕРКЕ", "=" * 60, ""]

    if index_meta:
        built = index_meta.get("built_at", "неизвестно")
        lines.append(f"База реестра составлена: {built}")
        lines.append(f"Записей в реестре: {index_meta.get('entry_count', '?')}")
        lines.append("")

    if not matches:
        lines.append("Совпадений не найдено.")
        lines.append("")
        lines.append(
            "Это относится только к тем данным, которые были загружены. "
            "Разделы, по которым выгрузка не делалась, не проверялись."
        )
        return "\n".join(lines)

    stats = summarize(matches)
    lines.append(
        f"Найдено совпадений: {stats['всего']} "
        f"(точных — {stats['точных']}, вероятных — {stats['вероятных']}) "
        f"по {stats['записей реестра']} записям реестра."
    )
    lines.append("")

    for confidence, title, note in (
        (
            EXACT,
            "ТОЧНЫЕ СОВПАДЕНИЯ",
            "Адрес или идентификатор совпал полностью.",
        ),
        (
            PROBABLE,
            "ВЕРОЯТНЫЕ СОВПАДЕНИЯ",
            "Совпало имя, но не всё остальное. Это повод проверить глазами, "
            "а не готовый вывод.",
        ),
    ):
        group = [m for m in matches if m.confidence == confidence]
        if not group:
            continue

        lines.append("")
        lines.append(title)
        lines.append("-" * 60)
        lines.append(note)
        lines.append("")

        by_entry: dict[int, list[Match]] = defaultdict(list)
        for match in group:
            by_entry[match.entry.entry_id].append(match)

        for number, (entry_id, entry_matches) in enumerate(sorted(by_entry.items()), 1):
            entry = entry_matches[0].entry
            lines.append(f"{number}. {_shorten(entry.description)}")
            lines.append(f"   Решение суда: {_shorten(entry.court, 150)}")
            lines.append("   Где найдено у вас:")
            for match in entry_matches:
                observation = match.observation
                where = f"      • {observation.platform}: {observation.trace_label}"
                if observation.occurred_at:
                    where += f", {observation.occurred_at}"
                lines.append(where)
                lines.append(f"        источник: {observation.source}")
                lines.append(f"        совпало: {observation.raw or observation.value}")
                if match.confidence == PROBABLE:
                    lines.append(f"        почему вероятное: {match.reason}")
            lines.append("")

    lines.append("")
    lines.append(WARNING)
    return "\n".join(lines)


def save(text: str, destination: str | Path) -> Path:
    """Сохранить отчёт в файл.

    Вызывается только по явному действию пользователя.
    """
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
