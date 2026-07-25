"""Разбор HTML-таблицы реестра на строки.

Таблица состоит из трёх колонок:

1. вид материалов («Информационная продукция», «Символика и атрибутика»);
2. описание с идентификаторами — самая содержательная колонка;
3. реквизиты судебного решения.

Разборщик собран на ``html.parser`` из стандартной библиотеки: сторонние
пакеты не нужны, а значит и упаковка в один ``.exe`` останется простой.
HTML от LibreOffice грязнее исходного (лишние ``span``, стили), поэтому
разборщик опирается только на структуру ``tr``/``td`` и игнорирует оформление.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .normalize import (
    PLATFORM_BY_DOMAIN,
    Identifier,
    find_bare_domains,
    find_bare_handles,
    find_numeric_ids,
    find_urls,
    is_checkable_domain,
    normalize_host,
    parse_url,
)

# Заголовочная строка таблицы — не запись реестра.
_HEADER_MARKERS = (
    "вид экстремистских материалов",
    "наименование суда",
)

# Признак отменённого судебного решения. В исходном файле у таких записей
# сам идентификатор уже вычищен — остаётся только слово «Отменено»
# и реквизиты. Учитывать их в проверке не нужно.
_CANCELLED_RE = re.compile(r"\bотменен[оа]\b", re.I)

# Разделы реестра вне соцсетей — по ним возможна только ручная проверка.
_OFFLINE_MARKERS = (
    "книжное издание",
    "книжные издания",
    "печатное издание",
    "печатные издания",
    "музыкальн",
    "аудиозапис",
    "cd-r",
    "dvd",
    "брошюр",
    "листовк",
)


@dataclass
class RegistryRow:
    """Одна строка реестра — одно судебное решение."""

    row_index: int
    material_type: str
    description: str
    court: str
    identifiers: list[Identifier] = field(default_factory=list)

    @property
    def is_cancelled(self) -> bool:
        """Решение отменено — запись в проверке не участвует."""
        return bool(_CANCELLED_RE.search(self.description)) or bool(
            _CANCELLED_RE.search(self.material_type)
        )

    @property
    def is_offline_material(self) -> bool:
        """Книги, музыка, диски — автоматической проверке не поддаются."""
        low = self.description.lower()
        return any(marker in low for marker in _OFFLINE_MARKERS)

    @property
    def is_header(self) -> bool:
        low = (self.material_type + " " + self.court).lower()
        return any(marker in low for marker in _HEADER_MARKERS)


class _TableParser(HTMLParser):
    """Собирает содержимое ячеек таблицы, сохраняя ссылки из атрибутов href."""

    # Блочные теги, на границе которых нужен перенос строки, иначе соседние
    # абзацы склеиваются и адрес слипается с текстом.
    _BLOCK_TAGS = {"p", "br", "div", "li", "tr", "td", "h1", "h2", "h3"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag == "td" or tag == "th":
            self._cell = []
        elif tag in self._BLOCK_TAGS and self._cell is not None:
            self._cell.append("\n")
        elif tag == "a" and self._cell is not None:
            # Адрес в href часто полнее видимого текста ссылки.
            for name, value in attrs:
                if name == "href" and value:
                    self._cell.append(f" {value} ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag in self._BLOCK_TAGS and self._cell is not None:
            self._cell.append("\n")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def extract_identifiers(text: str) -> list[Identifier]:
    """Вытащить из описания все опознавательные признаки ресурса.

    Одна судебная запись часто перечисляет несколько «зеркал» одного и того
    же субъекта в разных соцсетях — все они попадают в один список.
    """
    found: list[Identifier] = []
    seen: set[str] = set()

    def add(identifiers: list[Identifier]) -> None:
        for ident in identifiers:
            if ident.key not in seen:
                seen.add(ident.key)
                found.append(ident)

    for url in find_urls(text):
        add(parse_url(url))

    for domain in find_bare_domains(text):
        host, notes = normalize_host(domain)
        # Домен известной площадки, названный без пути (просто «instagram.com»),
        # ресурс не опознаёт — пропускаем.
        if host in PLATFORM_BY_DOMAIN or not is_checkable_domain(host):
            continue
        add([Identifier("web", "domain", host, domain, tuple(notes))])

    for digits in find_numeric_ids(text):
        # Такие числа в реестре — всегда внутренние идентификаторы Telegram.
        add([Identifier("telegram", "numeric_id", digits, digits, ())])

    for handle in find_bare_handles(text):
        add([Identifier("telegram", "handle", handle.lower(), "@" + handle, ())])

    # Имя аккаунта иногда выглядит как домен («baj.media.by» в Instagram).
    # Если такое значение уже опознано как имя аккаунта, одноимённый «домен»
    # — это его двойник, а не отдельный сайт.
    handle_values = {i.value for i in found if i.kind == "handle"}
    return [
        ident
        for ident in found
        if not (ident.kind == "domain" and ident.value in handle_values)
    ]


def parse_registry(html_text: str) -> list[RegistryRow]:
    """Разобрать HTML реестра в список строк с извлечёнными идентификаторами."""
    parser = _TableParser()
    parser.feed(html_text)
    parser.close()

    rows: list[RegistryRow] = []
    for index, cells in enumerate(parser.rows):
        if not cells:
            continue
        cleaned = [_tidy(cell) for cell in cells]
        # Колонок может быть больше трёх, если в вёрстке есть объединения:
        # первая — вид, последняя — суд, всё между ними — описание.
        material_type = cleaned[0]
        court = cleaned[-1] if len(cleaned) > 1 else ""
        description = "\n".join(cleaned[1:-1]) if len(cleaned) > 2 else ""

        row = RegistryRow(
            row_index=index,
            material_type=material_type,
            description=description,
            court=court,
        )
        if row.is_header:
            continue
        if not row.is_cancelled:
            row.identifiers = extract_identifiers(description)
        rows.append(row)

    return rows
