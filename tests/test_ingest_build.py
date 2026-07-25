"""Проверки чтения исходного файла и сборки снапшота."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from registry.build import write_snapshot
from registry.ingest import IngestError, detect_format, load_html
from registry.parse import parse_registry
from tests.make_docx import build_docx

ROWS = [
    ["Вид экстремистских материалов", "Наименование", "Наименование суда"],
    [
        "Информационная продукция",
        "Telegram-канал, идентификатор https://t.me/testcanal и идентификатор (ID), "
        "содержащий последовательность цифр 1442177936",
        "Решение суда от 1 января 2021 года",
    ],
    [
        "Информационная продукция",
        "ссылка в гиперссылке",
        "Решение суда от 2 февраля 2022 года",
    ],
    [
        "Информационная продукция",
        "Отменено.",
        "Решение суда. Отменено определением суда.",
    ],
]


class TestDocx(unittest.TestCase):
    """Файл .docx разбирается напрямую, без LibreOffice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = build_docx(
            Path(self.tmp.name) / "registry.docx",
            ROWS,
            {"ссылка в гиперссылке": "https://vk.com/hidden_account"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_detected_as_docx_by_content(self):
        self.assertEqual(detect_format(self.path), "docx")

    def test_detected_even_when_extension_lies(self):
        """С сайта файл может прийти с расширением .doc, будучи .docx."""
        misnamed = self.path.with_suffix(".doc")
        misnamed.write_bytes(self.path.read_bytes())
        self.assertEqual(detect_format(misnamed), "docx")
        self.assertIn("testcanal", load_html(misnamed))

    def test_rows_and_identifiers_survive_the_round_trip(self):
        rows = parse_registry(load_html(self.path))
        self.assertEqual(len(rows), 3)  # заголовок отброшен
        keys = {i.key for i in rows[0].identifiers}
        self.assertEqual(
            keys, {"telegram:handle:testcanal", "telegram:numeric_id:1442177936"}
        )

    def test_address_is_read_from_hyperlink(self):
        rows = parse_registry(load_html(self.path))
        self.assertIn("vk:handle:hidden_account", {i.key for i in rows[1].identifiers})

    def test_cancelled_row_survives_as_cancelled(self):
        rows = parse_registry(load_html(self.path))
        self.assertTrue(rows[2].is_cancelled)


class TestUnsupported(unittest.TestCase):
    def test_missing_file_reports_clearly(self):
        with self.assertRaises(IngestError):
            load_html("/nonexistent/registry.doc")

    def test_binary_garbage_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "junk.bin"
            path.write_bytes(b"\x01\x02\x03\x04binary junk")
            with self.assertRaises(IngestError):
                load_html(path)


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = build_docx(
            Path(self.tmp.name) / "registry.docx", ROWS, {}
        )
        self.destination = Path(self.tmp.name) / "out" / "registry.db"
        rows = parse_registry(load_html(self.source))
        self.result = write_snapshot(rows, self.destination, self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_file_is_created(self):
        self.assertTrue(self.destination.is_file())

    def test_entries_and_identifiers_are_stored(self):
        connection = sqlite3.connect(self.destination)
        try:
            entries = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            identifiers = connection.execute(
                "SELECT COUNT(*) FROM identifiers"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(entries, 3)
        self.assertEqual(identifiers, self.result["identifiers"])

    def test_cancelled_entry_is_kept_but_flagged(self):
        connection = sqlite3.connect(self.destination)
        try:
            cancelled = connection.execute(
                "SELECT COUNT(*) FROM entries WHERE is_cancelled = 1"
            ).fetchone()[0]
            orphans = connection.execute(
                "SELECT COUNT(*) FROM identifiers i JOIN entries e ON e.id = i.entry_id"
                " WHERE e.is_cancelled = 1"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(cancelled, 1)
        self.assertEqual(orphans, 0, "у отменённых записей не должно быть признаков")

    def test_source_checksum_is_recorded(self):
        """Снапшот должен помнить, из какого файла он собран."""
        connection = sqlite3.connect(self.destination)
        try:
            meta = dict(connection.execute("SELECT key, value FROM snapshot_meta"))
        finally:
            connection.close()
        self.assertEqual(len(meta["source_sha256"]), 64)
        self.assertEqual(meta["source_name"], "registry.docx")

    def test_rebuild_replaces_previous_snapshot(self):
        rows = parse_registry(load_html(self.source))
        again = write_snapshot(rows, self.destination, self.source)
        self.assertEqual(again["entries"], self.result["entries"])


if __name__ == "__main__":
    unittest.main()
