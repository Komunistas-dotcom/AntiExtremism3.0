"""Проверки приведения адресов к единому виду."""

import unittest

from registry.normalize import (
    clean_raw_url,
    find_bare_domains,
    find_numeric_ids,
    is_checkable_domain,
    normalize_host,
    parse_url,
)


def keys(raw: str) -> set[str]:
    return {i.key for i in parse_url(raw)}


def single(raw: str):
    identifiers = parse_url(raw)
    assert len(identifiers) == 1, f"{raw!r} -> {[i.key for i in identifiers]}"
    return identifiers[0]


class TestCleaning(unittest.TestCase):
    def test_strips_sentence_punctuation(self):
        """В реестре 3014 адресов заканчиваются точкой, 1606 — точкой с запятой."""
        self.assertEqual(clean_raw_url("https://t.me/foo;"), "https://t.me/foo")
        self.assertEqual(clean_raw_url("https://t.me/foo,"), "https://t.me/foo")
        self.assertEqual(clean_raw_url("https://t.me/foo."), "https://t.me/foo")

    def test_stops_at_text_running_into_url(self):
        self.assertEqual(
            clean_raw_url("https://t.me/strokabelarus&nbsp;и 3"),
            "https://t.me/strokabelarus",
        )


class TestHost(unittest.TestCase):
    def test_fixes_cyrillic_inside_domain(self):
        """Домены латинские по стандарту — кириллица там всегда опечатка."""
        host, notes = normalize_host("vk.соm")
        self.assertEqual(host, "vk.com")
        self.assertTrue(notes)

    def test_strips_stacked_service_prefixes(self):
        """В реестре встречается m.www.youtube.com и www.m.facebook.com."""
        self.assertEqual(normalize_host("m.www.youtube.com")[0], "youtube.com")
        self.assertEqual(normalize_host("www.m.facebook.com")[0], "facebook.com")

    def test_corrects_known_typos(self):
        for wrong, right in [
            ("twiter.com", "x.com"),
            ("yotube.com", "youtube.com"),
            ("facebok.com", "facebook.com"),
            ("instgram.com", "instagram.com"),
            ("tiktok.eom", "tiktok.com"),
            ("vm.tiktok.com", "tiktok.com"),
        ]:
            with self.subTest(wrong=wrong):
                self.assertEqual(normalize_host(wrong)[0], right)

    def test_does_not_strip_prefix_from_two_label_domain(self):
        """'m.by' — самостоятельный домен, а не поддомен."""
        self.assertEqual(normalize_host("m.by")[0], "m.by")


class TestTelegram(unittest.TestCase):
    def test_plain_handle(self):
        self.assertEqual(single("https://t.me/mozyr97pro").key, "telegram:handle:mozyr97pro")

    def test_preview_link_is_same_channel(self):
        """t.me/s/foo и t.me/foo — один и тот же канал."""
        self.assertEqual(single("https://t.me/s/foo").key, single("https://t.me/foo").key)

    def test_internal_numeric_id(self):
        self.assertIn("telegram:numeric_id:1436214594", keys("https://t.me/c/1436214594/256"))

    def test_cyrillic_c_marker_still_yields_numeric_id(self):
        """t.me/с/… с кириллической 'с' встречается в реестре 4 раза."""
        self.assertIn("telegram:numeric_id:1178902512", keys("https://t.me/с/1178902512/569"))

    def test_invite_links(self):
        self.assertEqual(single("https://t.me/+k9uKx4khfuhiMGU5").kind, "invite")
        self.assertEqual(single("https://t.me/joinchat/AAAA").kind, "invite")

    def test_homoglyph_in_invite_hash_is_fixed(self):
        self.assertIn("telegram:invite:k9uKx4khfuhiMGU5", keys("https://t.me/+k9uKх4khfuhiMGU5"))


class TestOtherPlatforms(unittest.TestCase):
    def test_instagram_handle_is_lowercased(self):
        self.assertEqual(single("https://www.instagram.com/Foo/").key, "instagram:handle:foo")

    def test_instagram_post_is_kept(self):
        """Конкретная публикация нужна: она встречается в лайках и сохранённом."""
        self.assertEqual(single("https://instagram.com/p/ABC123/").kind, "post")

    def test_vk_numeric_community(self):
        self.assertEqual(single("https://vk.com/public28199915").key, "vk:numeric_id:28199915")

    def test_vk_wall_points_at_owner(self):
        self.assertEqual(single("https://vk.com/wall-27464088_123").key, "vk:numeric_id:27464088")

    def test_facebook_numeric_profile(self):
        self.assertEqual(
            single("https://facebook.com/profile.php?id=100070530127003").key,
            "facebook:numeric_id:100070530127003",
        )

    def test_facebook_group(self):
        self.assertEqual(single("https://facebook.com/groups/somegroup").kind, "group")

    def test_tiktok_handle(self):
        self.assertEqual(single("https://tiktok.com/@antahon").key, "tiktok:handle:antahon")

    def test_youtube_channel_id(self):
        self.assertEqual(
            single("https://youtube.com/channel/UCg31").key, "youtube:channel_id:UCg31"
        )

    def test_youtube_video_is_kept(self):
        """История просмотров YouTube проверяема — ролики нужны в индексе."""
        self.assertEqual(single("https://www.youtube.com/watch?v=qF-wX10MHKU").key,
                         "youtube:video:qF-wX10MHKU")

    def test_youtu_be_short_link_is_a_video_not_a_channel(self):
        self.assertEqual(single("https://youtu.be/qF-wX10MHKU").key,
                         "youtube:video:qF-wX10MHKU")

    def test_ok_group(self):
        self.assertEqual(single("https://ok.ru/group/53245").key, "ok:numeric_id:53245")

    def test_twitter_and_x_are_the_same_platform(self):
        self.assertEqual(
            single("https://twitter.com/Dz_Kuchynski").key,
            single("https://x.com/Dz_Kuchynski").key,
        )


class TestMixedScriptNames(unittest.TestCase):
    """Смешанные написания разрешаются в оба варианта сразу."""

    def test_cyrillic_letter_in_domain_is_always_a_typo(self):
        """'к' в 'vк.соm' различима глазом, но в домене всё равно ошибка."""
        self.assertEqual(keys("https://vк.соm/krylyhalopa"), {"vk:handle:krylyhalopa"})

    def test_genuinely_cyrillic_name_typed_partly_in_latin(self):
        """«НАША» кириллицей + «MOBA» латиницей — верное чтение кириллическое."""
        self.assertIn("youtube:handle:нашамова", keys("https://www.youtube.com/@НАШАMOBA"))

    def test_pure_latin_handle_gets_no_cyrillic_twin(self):
        """Иначе индекс засоряется двойниками вроде 'моzуr97рrо'."""
        self.assertEqual(keys("https://t.me/mozyr97pro"), {"telegram:handle:mozyr97pro"})

    def test_pure_cyrillic_name_gets_no_latin_twin(self):
        self.assertEqual(
            keys("https://www.youtube.com/c/ОбществоГомель"),
            {"youtube:handle:обществогомель"},
        )


class TestLegacyAddressForms(unittest.TestCase):
    def test_odnoklassniki_old_group_address(self):
        self.assertEqual(
            single("https://m.ok.ru/dk?st.cmd=altGroupMain&st.groupId=64819888521256").key,
            "ok:numeric_id:64819888521256",
        )

    def test_facebook_legacy_page_prefix(self):
        self.assertEqual(
            single("https://www.facebook.com/pg/navinyVilejki/posts/").key,
            "facebook:handle:navinyvilejki",
        )

    def test_shortlink_is_kept_as_a_resource(self):
        """Домен сокращателя не признак, а конкретный короткий адрес — признак."""
        identifier = single("https://bit.ly/slovo-belarusov")
        self.assertEqual(identifier.key, "web:shortlink:bit.ly/slovo-belarusov")


class TestDomains(unittest.TestCase):
    def test_finds_domain_without_protocol(self):
        """Такие записи важны для проверки истории браузера."""
        self.assertIn("misanthropic.info", find_bare_domains("материалы: misanthropic.info"))

    def test_ignores_filenames_and_abbreviations(self):
        junk = "файл материал.wmv, index.php, 2011г. и стр.15"
        self.assertEqual(find_bare_domains(junk), [])

    def test_rejects_intermediary_hosts(self):
        """apps.apple.com есть в истории почти у всех — это не признак."""
        self.assertFalse(is_checkable_domain("apps.apple.com"))
        self.assertFalse(is_checkable_domain("54de5g.cdn.ampproject.org"))
        self.assertTrue(is_checkable_domain("charter97.org"))

    def test_mastodon_style_address_does_not_invent_a_domain(self):
        """https://kolektiva.social@abcbelarus — домен стоит до «собаки»."""
        self.assertEqual(
            keys("https://kolektiva.social@abcbelarus"), {"web:domain:kolektiva.social"}
        )


class TestNumericIds(unittest.TestCase):
    def test_reads_id_written_in_words(self):
        text = "идентификатор (ID), содержащий последовательность цифр 1442177936"
        self.assertEqual(find_numeric_ids(text), ["1442177936"])

    def test_reads_id_glued_to_the_word(self):
        """В реестре встречается «цифр1195470487» без пробела."""
        self.assertEqual(
            find_numeric_ids("последовательность цифр1195470487"), ["1195470487"]
        )


if __name__ == "__main__":
    unittest.main()
