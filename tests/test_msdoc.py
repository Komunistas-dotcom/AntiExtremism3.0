"""Проверки чтения двоичного .doc (Word 97-2003)."""

import unittest
from pathlib import Path

from registry.ingest import load_html
from registry.msdoc import (
    CompoundFile,
    DocError,
    _clean_field_codes,
    extract_table_rows,
)
from registry.parse import parse_registry

REAL_DOC = Path("data/spisok-ekstremistskix-materialov.doc")


class TestFieldCodes(unittest.TestCase):
    """Настоящий адрес спрятан в поле гиперссылки, а не в видимом тексте."""

    def test_hyperlink_address_is_extracted(self):
        text = '\x13 HYPERLINK "https://t.me/hidden" \x14видимый текст\x15'
        result = _clean_field_codes(text)
        self.assertIn("https://t.me/hidden", result)
        self.assertIn("видимый текст", result)

    def test_non_hyperlink_field_keeps_only_visible_text(self):
        text = "\x13 PAGE \\* MERGEFORMAT \x1412\x15"
        self.assertEqual(_clean_field_codes(text), "12")

    def test_plain_text_is_untouched(self):
        self.assertEqual(_clean_field_codes("обычный текст"), "обычный текст")

    def test_unterminated_field_does_not_lose_the_tail(self):
        self.assertIn("хвост", _clean_field_codes("начало\x13хвост"))


class TestTableMarkers(unittest.TestCase):
    """Ячейки и строки размечены символом \\x07."""

    def test_row_and_cells_are_split(self):
        rows = extract_table_rows("а\x07б\x07в\x07\x07г\x07д\x07е\x07\x07")
        self.assertEqual(rows, [["а", "б", "в"], ["г", "д", "е"]])

    def test_line_breaks_inside_a_cell_become_newlines(self):
        rows = extract_table_rows("первая\x0bвторая\x07конец\x07\x07")
        self.assertEqual(rows[0][0], "первая\nвторая")

    def test_paragraph_marks_inside_a_cell_become_newlines(self):
        rows = extract_table_rows("абзац\rещё\x07конец\x07\x07")
        self.assertEqual(rows[0][0], "абзац\nещё")

    def test_trailing_row_without_final_marker_is_kept(self):
        rows = extract_table_rows("а\x07б\x07")
        self.assertEqual(rows, [["а", "б"]])


class TestContainer(unittest.TestCase):
    def test_rejects_a_file_that_is_not_word(self):
        with self.assertRaises(DocError):
            CompoundFile(b"not an OLE2 file at all")


@unittest.skipUnless(REAL_DOC.is_file(), "нет файла реестра в data/")
class TestRealRegistry(unittest.TestCase):
    """Проверка на настоящем файле с сайта министерства."""

    @classmethod
    def setUpClass(cls):
        cls.rows = parse_registry(load_html(REAL_DOC))

    def test_registry_is_read_whole(self):
        self.assertGreater(len(self.rows), 5000)

    def test_identifiers_are_extracted_in_bulk(self):
        total = sum(len(r.identifiers) for r in self.rows)
        self.assertGreater(total, 10000)

    def test_known_record_is_found_with_both_identifiers(self):
        keys = {i.key for r in self.rows for i in r.identifiers}
        self.assertIn("telegram:handle:mozyr97pro", keys)
        self.assertIn("telegram:numeric_id:1442177936", keys)

    def test_cancelled_records_contribute_nothing(self):
        for row in self.rows:
            if row.is_cancelled:
                self.assertEqual(row.identifiers, [])

    def test_no_identifier_has_text_glued_to_it(self):
        """Признак «;сообщество» на конце означал бы, что чистка адреса сломалась."""
        for row in self.rows:
            for identifier in row.identifiers:
                self.assertNotIn(";", identifier.value)
                self.assertNotIn(" ", identifier.value)

    def test_court_column_looks_like_a_court_decision(self):
        with_court = sum(1 for r in self.rows if "ешение" in r.court or "уда" in r.court)
        self.assertGreater(with_court, len(self.rows) * 0.9)


if __name__ == "__main__":
    unittest.main()
