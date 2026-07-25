"""Приведение исходного файла реестра к HTML, независимо от его формата.

Министерство информации публикует список в формате ``.doc``. Требовать от
человека вручную конвертировать файл нельзя, поэтому конвертацию делает сам
сборщик: ``.doc``/``.docx`` прогоняются через LibreOffice в режиме без окна,
а дальше работает общий разборщик HTML-таблицы.

Форматы ``.html`` и ``.md`` (выгрузка из Joplin) поддерживаются напрямую —
они уже содержат ту же самую таблицу.
"""

from __future__ import annotations

import html as html_module
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

# LibreOffice в разных сборках называется по-разному.
_SOFFICE_NAMES = ("soffice", "libreoffice")

_DIRECT_SUFFIXES = {".html", ".htm", ".md", ".markdown", ".txt"}
_OFFICE_SUFFIXES = {".doc", ".docx", ".odt", ".rtf", ".fodt"}


class IngestError(RuntimeError):
    """Исходный файл не удалось привести к HTML."""


def find_soffice() -> str | None:
    """Путь к LibreOffice, если он установлен."""
    for name in _SOFFICE_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


def detect_format(path: Path) -> str:
    """Определить формат по содержимому, а не только по расширению.

    Файлы с сайта министерства нередко имеют расширение ``.doc``, будучи на
    самом деле ``.docx`` (zip) или вовсе HTML — расширению доверять нельзя.
    """
    with path.open("rb") as fh:
        head = fh.read(8)

    if head.startswith(b"PK\x03\x04"):
        return "docx"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # OLE2 — настоящий Word 97-2003
        return "doc"
    if head.lstrip()[:1] in (b"<", b"*", b"#"):
        return "html"
    return path.suffix.lower().lstrip(".") or "unknown"


def convert_with_soffice(path: Path, workdir: Path) -> Path:
    """Конвертировать документ в HTML через LibreOffice."""
    soffice = find_soffice()
    if soffice is None:
        raise IngestError(
            f"Файл {path.name} нужно преобразовать из формата Word, "
            "но LibreOffice не найден. Установите LibreOffice либо сохраните "
            "файл как .html и передайте его."
        )

    profile = workdir / "profile"
    command = [
        soffice,
        "--headless",
        "--norestore",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to",
        "html:HTML (StarWriter)",
        "--outdir",
        str(workdir),
        str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=600, check=False, text=True
        )
    except subprocess.TimeoutExpired as exc:
        raise IngestError("LibreOffice не ответил за 10 минут.") from exc

    produced = list(workdir.glob("*.html"))
    if not produced:
        raise IngestError(
            f"LibreOffice не смог преобразовать {path.name}.\n"
            f"Код возврата: {result.returncode}\n{result.stderr[:800]}"
        )
    return produced[0]


# --------------------------------------------------------------------------
# .docx — разбор без сторонних библиотек
# --------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _docx_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    """Соответствие идентификаторов ссылок их адресам."""
    try:
        raw = archive.read("word/_rels/document.xml.rels")
    except KeyError:
        return {}
    links: dict[str, str] = {}
    for node in ElementTree.fromstring(raw):
        identifier = node.get("Id")
        target = node.get("Target")
        if identifier and target:
            links[identifier] = target
    return links


def _docx_cell_text(cell: ElementTree.Element, links: dict[str, str]) -> str:
    """Текст ячейки вместе с адресами из гиперссылок."""
    pieces: list[str] = []
    for node in cell.iter():
        tag = node.tag
        if tag == f"{_W}hyperlink":
            target = links.get(node.get(f"{_R}id", ""))
            if target:
                # Адрес в гиперссылке часто полнее видимого текста.
                pieces.append(f" {target} ")
        elif tag == f"{_W}t":
            pieces.append(node.text or "")
        elif tag in (f"{_W}br", f"{_W}p", f"{_W}tab"):
            pieces.append("\n")
    return "".join(pieces)


def docx_to_html(path: Path) -> str:
    """Превратить таблицы документа .docx в простой HTML.

    Разбор идёт стандартной библиотекой, поэтому для самого частого случая
    LibreOffice не нужен вовсе.
    """
    with zipfile.ZipFile(path) as archive:
        try:
            document = archive.read("word/document.xml")
        except KeyError as exc:
            raise IngestError(
                f"{path.name}: это не документ Word (нет word/document.xml)."
            ) from exc
        links = _docx_relationships(archive)

    root = ElementTree.fromstring(document)
    out: list[str] = ["<html><body><table>"]
    for table in root.iter(f"{_W}tbl"):
        for row in table.findall(f"{_W}tr"):
            out.append("<tr>")
            for cell in row.findall(f"{_W}tc"):
                text = _docx_cell_text(cell, links)
                out.append("<td>" + html_module.escape(text) + "</td>")
            out.append("</tr>")
    out.append("</table></body></html>")
    return "".join(out)


def _read_text(path: Path) -> str:
    """Прочитать файл, подобрав кодировку.

    HTML от LibreOffice бывает в UTF-8, а старые выгрузки — в windows-1251.
    """
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1251", "koi8-r"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_html(path: str | Path) -> str:
    """Вернуть HTML-содержимое реестра из файла любого поддерживаемого формата."""
    path = Path(path)
    if not path.is_file():
        raise IngestError(f"Файл не найден: {path}")

    fmt = detect_format(path)
    suffix = path.suffix.lower()

    if fmt == "html" or suffix in _DIRECT_SUFFIXES:
        return _read_text(path)

    if fmt == "docx":
        # Самый частый случай: файл разбирается напрямую, без LibreOffice.
        return docx_to_html(path)

    if fmt == "doc" or suffix in _OFFICE_SUFFIXES:
        with tempfile.TemporaryDirectory(prefix="registry-ingest-") as tmp:
            workdir = Path(tmp)
            # LibreOffice ориентируется на расширение — даём ему корректное.
            staged = workdir / f"registry.{ 'docx' if fmt == 'docx' else 'doc' }"
            staged.write_bytes(path.read_bytes())
            produced = convert_with_soffice(staged, workdir)
            return _read_text(produced)

    raise IngestError(
        f"Неизвестный формат файла {path.name} (определён как {fmt!r}). "
        "Поддерживаются .doc, .docx, .odt, .rtf, .html, .md"
    )
