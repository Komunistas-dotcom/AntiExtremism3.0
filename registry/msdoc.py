"""Чтение двоичного .doc (Word 97-2003) без сторонних библиотек.

Министерство информации публикует список именно в этом формате. Разбор
сделан на стандартной библиотеке по двум причинам: LibreOffice нельзя
вложить внутрь одного ``.exe``, и требовать от человека установки офисного
пакета ради обновления базы неправильно.

Формат состоит из двух слоёв:

1. **Контейнер OLE2** — файловая система внутри файла. Из неё нужны потоки
   ``WordDocument`` (текст) и ``0Table``/``1Table`` (служебные таблицы).
2. **Текст документа** собирается по «таблице кусков» (piece table): текст
   лежит в файле не подряд, а фрагментами, часть в UTF-16, часть в
   однобайтовой кодировке.

Разметка таблицы берётся из самого текста: в формате Word конец ячейки и
конец строки обозначаются символом ``\\x07``, два подряд — конец строки.
"""

from __future__ import annotations

import struct
from pathlib import Path

_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FREE_SECTOR = 0xFFFFFFFF
_END_OF_CHAIN = 0xFFFFFFFE
_MAX_SECTOR = 0xFFFFFFFA

# Служебные символы разметки в тексте Word.
CELL_MARK = "\x07"  # конец ячейки; два подряд — конец строки таблицы
LINE_BREAK = "\x0b"  # перенос строки внутри абзаца
PARAGRAPH_MARK = "\r"
FIELD_BEGIN = "\x13"
FIELD_SEPARATOR = "\x14"
FIELD_END = "\x15"

# Исключения кодировки для однобайтовых кусков (MS-DOC, 2.4.1).
_COMPRESSED_EXCEPTIONS = {
    0x82: "‚", 0x83: "ƒ", 0x84: "„", 0x85: "…",
    0x86: "†", 0x87: "‡", 0x88: "ˆ", 0x89: "‰",
    0x8A: "Š", 0x8B: "‹", 0x8C: "Œ", 0x91: "‘",
    0x92: "’", 0x93: "“", 0x94: "”", 0x95: "•",
    0x96: "–", 0x97: "—", 0x98: "˜", 0x99: "™",
    0x9A: "š", 0x9B: "›", 0x9C: "œ", 0x9F: "Ÿ",
}


class DocError(RuntimeError):
    """Файл .doc не удалось прочитать."""


class CompoundFile:
    """Минимальный читатель контейнера OLE2 (Compound File Binary)."""

    def __init__(self, data: bytes) -> None:
        if not data.startswith(_OLE_SIGNATURE):
            raise DocError("Это не файл Word 97-2003 (нет подписи OLE2).")
        self._data = data
        self._sector_size = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        self._mini_sector_size = 1 << struct.unpack_from("<H", data, 0x20)[0]
        self._mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
        self._fat = self._read_fat()
        self._mini_fat = self._read_mini_fat()
        self.entries = self._read_directory()
        self._mini_stream = self._read_mini_stream()

    # --- низкий уровень ---------------------------------------------------

    def _sector(self, index: int) -> bytes:
        start = (index + 1) * self._sector_size
        chunk = self._data[start : start + self._sector_size]
        if len(chunk) < self._sector_size:
            raise DocError("Файл обрывается: повреждён или скачан не полностью.")
        return chunk

    def _chain(self, start: int, fat: list[int]) -> list[int]:
        sectors: list[int] = []
        seen: set[int] = set()
        current = start
        while current < _MAX_SECTOR:
            if current in seen or current >= len(fat):
                break  # зацикленная или битая цепочка
            seen.add(current)
            sectors.append(current)
            current = fat[current]
        return sectors

    def _read_fat(self) -> list[int]:
        data = self._data
        fat_count = struct.unpack_from("<I", data, 0x2C)[0]
        difat = list(struct.unpack_from("<109I", data, 0x4C))

        # Продолжение списка FAT-секторов, если их больше 109.
        extra_count = struct.unpack_from("<I", data, 0x48)[0]
        next_sector = struct.unpack_from("<I", data, 0x44)[0]
        per_sector = self._sector_size // 4
        while extra_count > 0 and next_sector < _MAX_SECTOR:
            values = struct.unpack(f"<{per_sector}I", self._sector(next_sector))
            difat.extend(values[:-1])
            next_sector = values[-1]
            extra_count -= 1

        fat: list[int] = []
        for sector in difat[:fat_count]:
            if sector >= _MAX_SECTOR:
                continue
            fat.extend(struct.unpack(f"<{per_sector}I", self._sector(sector)))
        return fat

    def _read_mini_fat(self) -> list[int]:
        start = struct.unpack_from("<I", self._data, 0x3C)[0]
        count = struct.unpack_from("<I", self._data, 0x40)[0]
        if count == 0 or start >= _MAX_SECTOR:
            return []
        per_sector = self._sector_size // 4
        mini_fat: list[int] = []
        for sector in self._chain(start, self._fat)[:count]:
            mini_fat.extend(struct.unpack(f"<{per_sector}I", self._sector(sector)))
        return mini_fat

    def _read_directory(self) -> dict[str, tuple[int, int]]:
        start = struct.unpack_from("<I", self._data, 0x30)[0]
        raw = b"".join(self._sector(s) for s in self._chain(start, self._fat))
        entries: dict[str, tuple[int, int]] = {}
        self._root: tuple[int, int] = (0, 0)
        for offset in range(0, len(raw) - 127, 128):
            entry = raw[offset : offset + 128]
            name_length = struct.unpack_from("<H", entry, 0x40)[0]
            if name_length < 2:
                continue
            name = entry[: name_length - 2].decode("utf-16-le", errors="replace")
            kind = entry[0x42]
            first = struct.unpack_from("<I", entry, 0x74)[0]
            size = struct.unpack_from("<Q", entry, 0x78)[0]
            if kind == 5:  # корневая запись хранит поток малых объектов
                self._root = (first, size)
            elif kind == 2:
                entries[name] = (first, size)
        return entries

    def _read_mini_stream(self) -> bytes:
        first, size = self._root
        if size == 0 or first >= _MAX_SECTOR:
            return b""
        return b"".join(self._sector(s) for s in self._chain(first, self._fat))[:size]

    # --- открытый интерфейс ----------------------------------------------

    def read(self, name: str) -> bytes:
        """Прочитать поток по имени."""
        if name not in self.entries:
            raise DocError(f"В файле нет потока {name!r}.")
        first, size = self.entries[name]
        if size < self._mini_cutoff:
            step = self._mini_sector_size
            chunks = [
                self._mini_stream[s * step : (s + 1) * step]
                for s in self._chain(first, self._mini_fat)
            ]
        else:
            chunks = [self._sector(s) for s in self._chain(first, self._fat)]
        return b"".join(chunks)[:size]


def _decode_compressed(raw: bytes) -> str:
    """Однобайтовый кусок текста.

    Кириллица так не хранится — Word пакует в один байт только латиницу,
    поэтому здесь достаточно cp1252 с исключениями из спецификации.
    """
    return "".join(
        _COMPRESSED_EXCEPTIONS.get(byte, chr(byte)) if byte >= 0x80 else chr(byte)
        for byte in raw
    )


def extract_text(data: bytes) -> str:
    """Собрать текст основной части документа по таблице кусков."""
    container = CompoundFile(data)
    document = container.read("WordDocument")

    if struct.unpack_from("<H", document, 0)[0] != 0xA5EC:
        raise DocError("Повреждён заголовок документа Word.")

    # Бит 9 указывает, в каком из двух потоков лежат служебные таблицы.
    flags = struct.unpack_from("<H", document, 0x0A)[0]
    table_name = "1Table" if (flags >> 9) & 1 else "0Table"
    if table_name not in container.entries:
        table_name = "0Table" if table_name == "1Table" else "1Table"
    table = container.read(table_name)

    # Длина основной части: остальное — сноски, колонтитулы, примечания.
    main_length = struct.unpack_from("<I", document, 0x004C)[0]
    clx_offset, clx_length = struct.unpack_from("<II", document, 0x01A2)
    clx = table[clx_offset : clx_offset + clx_length]
    if not clx:
        raise DocError("В файле не найдена таблица кусков текста.")

    # Пропускаем блоки свойств, доходим до таблицы кусков.
    position = 0
    while position < len(clx) and clx[position] == 1:
        size = struct.unpack_from("<h", clx, position + 1)[0]
        position += 3 + size
    if position >= len(clx) or clx[position] != 2:
        raise DocError("Неожиданное строение таблицы кусков.")

    plc_length = struct.unpack_from("<I", clx, position + 1)[0]
    plc = clx[position + 5 : position + 5 + plc_length]
    piece_count = (len(plc) - 4) // 12
    if piece_count <= 0:
        raise DocError("Таблица кусков пуста.")

    positions = struct.unpack_from(f"<{piece_count + 1}I", plc, 0)
    descriptors_at = 4 * (piece_count + 1)

    pieces: list[str] = []
    for index in range(piece_count):
        offset = struct.unpack_from("<I", plc, descriptors_at + index * 8 + 2)[0]
        length = positions[index + 1] - positions[index]
        if offset & 0x40000000:
            start = (offset & ~0x40000000) // 2
            pieces.append(_decode_compressed(document[start : start + length]))
        else:
            raw = document[offset : offset + length * 2]
            pieces.append(raw.decode("utf-16-le", errors="replace"))

    return "".join(pieces)[:main_length] if main_length else "".join(pieces)


def _clean_field_codes(text: str) -> str:
    """Раскрыть поля Word, вытащив из гиперссылок настоящие адреса.

    Поле выглядит как ``\\x13 HYPERLINK "адрес" \\x14 видимый текст \\x15``.
    Адрес в нём часто полнее видимого текста, поэтому он тоже идёт в разбор.
    """
    out: list[str] = []
    position = 0
    while position < len(text):
        begin = text.find(FIELD_BEGIN, position)
        if begin < 0:
            out.append(text[position:])
            break
        out.append(text[position:begin])

        separator = text.find(FIELD_SEPARATOR, begin)
        end = text.find(FIELD_END, begin)
        if end < 0:
            out.append(text[begin + 1 :])
            break

        instruction = text[begin + 1 : separator if 0 < separator < end else end]
        if "HYPERLINK" in instruction.upper():
            parts = instruction.split('"')
            if len(parts) > 1:
                out.append(" " + parts[1] + " ")
        if 0 < separator < end:
            out.append(text[separator + 1 : end])
        position = end + 1
    return "".join(out)


def extract_table_rows(text: str) -> list[list[str]]:
    """Разбить текст документа на строки и ячейки таблицы.

    Конец ячейки в формате Word — символ ``\\x07``; идущий сразу за ним
    второй такой же символ означает конец строки таблицы.
    """
    text = _clean_field_codes(text)
    rows: list[list[str]] = []
    cells: list[str] = []
    buffer: list[str] = []

    for char in text:
        if char == CELL_MARK:
            if not buffer and cells:
                # Пустая «ячейка» сразу после предыдущей — это конец строки.
                rows.append(cells)
                cells = []
            else:
                cells.append("".join(buffer))
            buffer = []
        elif char in (LINE_BREAK, PARAGRAPH_MARK):
            buffer.append("\n")
        else:
            buffer.append(char)

    if cells:
        rows.append(cells)
    return rows


def doc_to_html(path: Path) -> str:
    """Превратить таблицы файла .doc в простой HTML.

    Дальше работает тот же разборщик, что и для остальных форматов.
    """
    import html as html_module

    rows = extract_table_rows(extract_text(Path(path).read_bytes()))
    out = ["<html><body><table>"]
    for row in rows:
        out.append("<tr>")
        for cell in row:
            out.append("<td>" + html_module.escape(cell) + "</td>")
        out.append("</tr>")
    out.append("</table></body></html>")
    return "".join(out)
