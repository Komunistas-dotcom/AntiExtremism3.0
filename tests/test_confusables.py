"""Проверки исправления омоглифов.

Это самая ответственная часть сборщика: ошибка в одну сторону даёт
пропущенное совпадение (ложное «у вас чисто»), в другую — испорченную
запись реестра.
"""

import unittest

from registry.confusables import fix_segment, has_homoglyphs, latin_variant, to_latin


class TestDetection(unittest.TestCase):
    def test_finds_cyrillic_pretending_to_be_latin(self):
        self.assertTrue(has_homoglyphs("сom"))  # кириллическая 'с'
        self.assertTrue(has_homoglyphs("раra"))  # кириллические 'р', 'а'

    def test_pure_latin_is_clean(self):
        self.assertFalse(has_homoglyphs("channel"))
        self.assertFalse(has_homoglyphs("bnr100by"))


class TestFixSegment(unittest.TestCase):
    """Смешанные сегменты исправляем, честные кириллические — нет."""

    def test_fixes_mixed_script_typos(self):
        cases = {
            "раra2022": "para2022",  # р, а — кириллица
            "bnr100bу": "bnr100by",  # у — кириллица
            "chаnnel": "channel",  # а — кириллица
            "publiс28199915": "public28199915",  # с — кириллица
            "TОRBAND": "TORBAND",  # О — кириллица
            "TORВAND": "TORBAND",  # В — кириллица
            "UCО8YYPY2GA": "UCO8YYPY2GA",  # О внутри идентификатора канала
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                fixed, changed = fix_segment(source)
                self.assertTrue(changed, f"{source!r} должен был исправиться")
                self.assertEqual(fixed, expected)

    def test_keeps_genuine_cyrillic_names(self):
        """Настоящее кириллическое название портить нельзя."""
        for name in ("ОбществоГомель", "натальяалехнович", "АнархистыБеларуси"):
            with self.subTest(name=name):
                fixed, changed = fix_segment(name)
                self.assertFalse(changed, f"{name!r} трогать не следовало")
                self.assertEqual(fixed, name)

    def test_keeps_plain_latin_untouched(self):
        fixed, changed = fix_segment("mozyr97pro")
        self.assertFalse(changed)
        self.assertEqual(fixed, "mozyr97pro")

    def test_ambiguous_all_homoglyph_segment_is_left_alone(self):
        """Сегмент 'с' целиком из омоглифов — решать нельзя, оставляем."""
        fixed, changed = fix_segment("с")
        self.assertFalse(changed)
        # но латинский вариант должен быть доступен отдельно
        self.assertEqual(latin_variant("с"), "c")


class TestHomoglyphTable(unittest.TestCase):
    def test_table_excludes_distinguishable_lowercase_pairs(self):
        """Строчные 'н', 'т', 'в', 'к', 'м' различимы глазом — их менять нельзя.

        Иначе кириллические названия вроде 'ветка' превратятся в мусор.
        """
        for char in ("н", "т", "в", "к", "м", "б", "ж", "щ"):
            with self.subTest(char=char):
                self.assertEqual(to_latin(char), char)

    def test_table_includes_indistinguishable_uppercase_pairs(self):
        self.assertEqual(to_latin("НТВКМ"), "HTBKM")

    def test_latin_variant_returns_none_when_nothing_changes(self):
        self.assertIsNone(latin_variant("plain"))


if __name__ == "__main__":
    unittest.main()
