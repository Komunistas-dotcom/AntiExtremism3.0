"""Сборка настоящего файла .docx для тестов, без сторонних библиотек.

Нужна, чтобы проверять разбор .docx на честном файле, а не на подделке.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_docx(path: Path, rows: list[list[str]], hyperlinks: dict[str, str]) -> Path:
    """Создать .docx с одной таблицей.

    ``hyperlinks`` — соответствие «текст в ячейке -> адрес», чтобы проверить,
    что адрес читается из гиперссылки, а не только из видимого текста.
    """
    rels = [
        f'<Relationship Id="rId{i + 10}" Type="{_R_NS}/hyperlink"'
        f' Target="{_escape(url)}" TargetMode="External"/>'
        for i, url in enumerate(hyperlinks.values())
    ]
    link_ids = {text: f"rId{i + 10}" for i, text in enumerate(hyperlinks)}

    body = []
    for row in rows:
        body.append(f"<w:tr xmlns:w='{_W_NS}'>")
        for cell in row:
            runs = []
            if cell in link_ids:
                runs.append(
                    f"<w:hyperlink xmlns:r='{_R_NS}' r:id='{link_ids[cell]}'>"
                    f"<w:r><w:t>{_escape(cell)}</w:t></w:r></w:hyperlink>"
                )
            else:
                runs.append(f"<w:r><w:t>{_escape(cell)}</w:t></w:r>")
            body.append(f"<w:tc><w:p>{''.join(runs)}</w:p></w:tc>")
        body.append("</w:tr>")

    document = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<w:document xmlns:w='{_W_NS}' xmlns:r='{_R_NS}'><w:body>"
        f"<w:tbl>{''.join(body)}</w:tbl></w:body></w:document>"
    )

    document_rels = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
        f"{''.join(rels)}</Relationships>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
    return path
