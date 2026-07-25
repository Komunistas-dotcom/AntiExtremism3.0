"""Сопоставление следов пользователя с реестром.

Движок принимает «наблюдения» и ищет их в снапшоте реестра. Совпадения
делятся на два разряда и **никогда не смешиваются**:

* **точное** — нормализованные значения совпали полностью, при той же
  площадке и том же типе признака. Это факт;
* **вероятное** — совпало значение, но не всё остальное (другая площадка,
  другой тип признака). Это повод посмотреть глазами, а не находка.

Разделение принципиально. Ложное срабатывание в таком продукте пугает
человека на ровном месте, а свалка из точных и сомнительных совпадений
делает отчёт бесполезным.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .observation import Observation

EXACT = "exact"
PROBABLE = "probable"

CONFIDENCE_LABELS = {
    EXACT: "точное совпадение",
    PROBABLE: "вероятное совпадение",
}

# Типы признаков, которые нельзя сопоставлять между площадками: числовые
# идентификаторы в разных сетях никак не связаны, и случайное совпадение
# двух длинных чисел не значит ничего.
_PLATFORM_BOUND_KINDS = {"numeric_id", "channel_id", "video", "post", "invite"}

# Слишком короткие имена дают случайные совпадения между площадками.
_MIN_CROSS_PLATFORM_LENGTH = 5


@dataclass(frozen=True)
class RegistryEntry:
    """Запись реестра — одно судебное решение."""

    entry_id: int
    material_type: str
    description: str
    court: str
    is_offline: bool


@dataclass(frozen=True)
class Match:
    """Совпадение следа пользователя с записью реестра."""

    observation: Observation
    entry: RegistryEntry
    platform: str
    kind: str
    value: str
    registry_raw: str
    confidence: str
    reason: str

    @property
    def confidence_label(self) -> str:
        return CONFIDENCE_LABELS.get(self.confidence, self.confidence)


class RegistryIndex:
    """Снапшот реестра, загруженный в память для быстрого поиска.

    Идентификаторов около одиннадцати тысяч, поэтому держать их в памяти
    проще и быстрее, чем ходить в базу на каждое наблюдение.
    """

    def __init__(self, snapshot: str | Path) -> None:
        self.path = Path(snapshot)
        if not self.path.is_file():
            raise FileNotFoundError(f"Файл базы реестра не найден: {self.path}")

        self._entries: dict[int, RegistryEntry] = {}
        self._exact: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)
        self._by_value: dict[str, list[tuple[str, str, int, str]]] = defaultdict(list)
        self.meta: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            self.meta = dict(connection.execute("SELECT key, value FROM snapshot_meta"))

            for row in connection.execute(
                "SELECT id, material_type, description, court, is_offline"
                " FROM entries WHERE is_cancelled = 0"
            ):
                self._entries[row[0]] = RegistryEntry(
                    entry_id=row[0],
                    material_type=row[1],
                    description=row[2],
                    court=row[3],
                    is_offline=bool(row[4]),
                )

            for entry_id, platform, kind, value, raw in connection.execute(
                "SELECT entry_id, platform, kind, value, raw FROM identifiers"
            ):
                if entry_id not in self._entries:
                    continue  # отменённая запись
                self._exact[(platform, kind, value)].append((entry_id, raw))
                self._by_value[value].append((platform, kind, entry_id, raw))
        finally:
            connection.close()

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def identifier_count(self) -> int:
        return sum(len(v) for v in self._exact.values())

    def entry(self, entry_id: int) -> RegistryEntry:
        return self._entries[entry_id]

    def find_exact(self, platform: str, kind: str, value: str):
        return self._exact.get((platform, kind, value), [])

    def find_by_value(self, value: str):
        return self._by_value.get(value, [])


class MatchEngine:
    """Ищет наблюдения пользователя в реестре."""

    def __init__(self, index: RegistryIndex) -> None:
        self.index = index

    def match_one(self, observation: Observation) -> list[Match]:
        """Найти совпадения для одного следа."""
        if not observation.value:
            return []

        matches: list[Match] = []
        seen_entries: set[int] = set()

        for entry_id, raw in self.index.find_exact(
            observation.platform, observation.kind, observation.value
        ):
            seen_entries.add(entry_id)
            matches.append(
                Match(
                    observation=observation,
                    entry=self.index.entry(entry_id),
                    platform=observation.platform,
                    kind=observation.kind,
                    value=observation.value,
                    registry_raw=raw,
                    confidence=EXACT,
                    reason="значение совпадает полностью",
                )
            )

        matches.extend(self._probable(observation, seen_entries))
        return matches

    def _probable(
        self, observation: Observation, seen_entries: set[int]
    ) -> list[Match]:
        """Совпадения, требующие проверки глазами."""
        if observation.kind in _PLATFORM_BOUND_KINDS:
            # Числовой идентификатор осмыслен только внутри своей площадки.
            return []
        if len(observation.value) < _MIN_CROSS_PLATFORM_LENGTH:
            return []

        matches: list[Match] = []
        for platform, kind, entry_id, raw in self.index.find_by_value(observation.value):
            if entry_id in seen_entries:
                continue
            if kind in _PLATFORM_BOUND_KINDS:
                continue
            if platform == observation.platform and kind == observation.kind:
                continue
            seen_entries.add(entry_id)
            if platform == observation.platform:
                reason = f"то же имя, но в реестре оно записано как «{kind}»"
            else:
                reason = (
                    f"такое же имя значится в реестре на площадке «{platform}» — "
                    "у одного и того же субъекта часто несколько «зеркал»"
                )
            matches.append(
                Match(
                    observation=observation,
                    entry=self.index.entry(entry_id),
                    platform=platform,
                    kind=kind,
                    value=observation.value,
                    registry_raw=raw,
                    confidence=PROBABLE,
                    reason=reason,
                )
            )
        return matches

    def match_all(self, observations) -> list[Match]:
        """Найти совпадения для набора следов.

        Точные совпадения идут первыми: именно с них человек начинает
        разбираться, а вероятные смотрит потом.
        """
        matches: list[Match] = []
        for observation in observations:
            matches.extend(self.match_one(observation))
        matches.sort(key=lambda m: (m.confidence != EXACT, m.observation.platform))
        return matches
