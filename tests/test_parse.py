"""Проверки разбора таблицы реестра."""

import unittest

from registry.parse import extract_identifiers, parse_registry

HEADER = (
    "<tr><td>Вид экстремистских материалов</td><td>Наименование</td>"
    "<td>Наименование суда, вынесшего решение</td></tr>"
)


def table(*rows: str) -> str:
    return "<html><body><table>" + HEADER + "".join(rows) + "</table></body></html>"


def row(description: str, court: str = "Решение суда от 1 января 2021 года") -> str:
    return f"<tr><td>Информационная продукция</td><td>{description}</td><td>{court}</td></tr>"


class TestTableStructure(unittest.TestCase):
    def test_header_row_is_not_a_record(self):
        rows = parse_registry(table(row("https://t.me/foo")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].court, "Решение суда от 1 января 2021 года")

    def test_paragraphs_do_not_glue_addresses_together(self):
        """Без переноса на границе абзацев два адреса слиплись бы в один."""
        rows = parse_registry(table(row("<p>https://t.me/one</p><p>https://t.me/two</p>")))
        self.assertEqual(
            {i.key for i in rows[0].identifiers},
            {"telegram:handle:one", "telegram:handle:two"},
        )

    def test_href_attribute_is_read(self):
        """Адрес в href бывает полнее видимого текста ссылки."""
        rows = parse_registry(table(row('<a href="https://t.me/hidden">тут</a>')))
        self.assertIn("telegram:handle:hidden", {i.key for i in rows[0].identifiers})


class TestCancelled(unittest.TestCase):
    """Отменённые решения в проверке не участвуют."""

    def test_cancelled_row_is_flagged_and_yields_nothing(self):
        rows = parse_registry(
            table(row("Отменено.", "Решение суда. Отменено определением суда."))
        )
        self.assertTrue(rows[0].is_cancelled)
        self.assertEqual(rows[0].identifiers, [])

    def test_cancelled_row_never_contributes_identifiers(self):
        """Даже если идентификатор в записи остался, он не попадает в индекс."""
        rows = parse_registry(
            table(row("Отменено. https://t.me/should_not_appear", "Решение суда."))
        )
        self.assertTrue(rows[0].is_cancelled)
        self.assertEqual(rows[0].identifiers, [])

    def test_normal_row_is_not_flagged(self):
        rows = parse_registry(table(row("https://t.me/foo")))
        self.assertFalse(rows[0].is_cancelled)


class TestMirrors(unittest.TestCase):
    """Одно решение суда часто перечисляет «зеркала» в разных соцсетях."""

    def test_all_mirrors_belong_to_one_record(self):
        description = (
            "Аккаунты: https://t.me/mirror, https://vk.com/mirror, "
            "https://www.instagram.com/mirror/, https://tiktok.com/@mirror, "
            "https://www.facebook.com/mirror"
        )
        rows = parse_registry(table(row(description)))
        self.assertEqual(len(rows), 1)
        platforms = {i.platform for i in rows[0].identifiers}
        self.assertEqual(platforms, {"telegram", "vk", "instagram", "tiktok", "facebook"})

    def test_duplicate_addresses_are_collapsed(self):
        rows = parse_registry(table(row("https://t.me/foo и https://t.me/s/foo")))
        self.assertEqual(len(rows[0].identifiers), 1)


class TestNumericOnly(unittest.TestCase):
    """Часть чатов указана только числовым ID, без ссылки."""

    def test_numeric_only_record_is_indexed(self):
        description = (
            "Telegram-чат, имеющий идентификатор (ID), "
            "содержащий последовательность цифр 1223603803"
        )
        identifiers = extract_identifiers(description)
        self.assertEqual([i.key for i in identifiers], ["telegram:numeric_id:1223603803"])

    def test_handle_and_numeric_id_are_both_kept(self):
        description = (
            'Telegram-группа "Мозырь 97%", имеющая идентификатор https://t.me/mozyr97pro '
            "и идентификатор (ID), содержащий последовательность цифр 1442177936"
        )
        keys = {i.key for i in extract_identifiers(description)}
        self.assertEqual(
            keys, {"telegram:handle:mozyr97pro", "telegram:numeric_id:1442177936"}
        )


class TestDeduplication(unittest.TestCase):
    def test_account_name_shaped_like_domain_is_not_also_a_site(self):
        """«baj.media.by» — имя аккаунта в Instagram, а не отдельный сайт."""
        description = 'Instagram-аккаунт "baj.media.by", идентификатор http://instagram.com/baj.media.by'
        identifiers = extract_identifiers(description)
        self.assertEqual([i.key for i in identifiers], ["instagram:handle:baj.media.by"])


class TestOfflineMaterials(unittest.TestCase):
    def test_books_are_marked_for_manual_check(self):
        rows = parse_registry(table(row("Книжное издание ”Особое мнение“, Брест, 2011г.")))
        self.assertTrue(rows[0].is_offline_material)


if __name__ == "__main__":
    unittest.main()
