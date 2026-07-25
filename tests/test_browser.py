"""Проверки чтения истории и закладок браузеров."""

import tempfile
import unittest
from pathlib import Path

from connectors.browser import (
    CHROMIUM,
    FIREFOX,
    BrowserProfile,
    collect,
    collect_all,
    discover_profiles,
    read_bookmarks,
    read_history,
)
from matching.engine import EXACT, MatchEngine
from tests.make_browser import build_chromium_profile, build_firefox_profile
from tests.test_matching import build_index

# Первый адрес есть в реестре, остальные — обычные.
HISTORY = [
    ("https://charter97.org/ru/news/2026/3/12/", "2026-03-12", 47),
    ("https://charter97.org/ru/news/other/", "2026-03-10", 3),
    ("https://example.com/page", "2026-03-01", 5),
    ("https://www.google.com/search?q=погода", "2026-02-20", 120),
]


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        build_chromium_profile(self.root / "chrome/Default", HISTORY, [])
        build_chromium_profile(self.root / "chrome/Profile 1", HISTORY, [])
        build_firefox_profile(self.root / "firefox/abc.default", HISTORY, [])

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_profile_is_found(self):
        """У браузера бывает несколько профилей, проверять надо каждый."""
        profiles = discover_profiles(
            {"Chrome": [self.root / "chrome"]}, [self.root / "firefox"]
        )
        names = {(p.browser, p.profile) for p in profiles}
        self.assertEqual(
            names, {("Chrome", "Default"), ("Chrome", "Profile 1"), ("Firefox", "abc.default")}
        )

    def test_missing_browser_is_skipped_quietly(self):
        self.assertEqual(discover_profiles({"Chrome": [self.root / "нет"]}, []), [])


class TestReading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_chromium_history_and_dates(self):
        directory = build_chromium_profile(self.root / "p", HISTORY)
        profile = BrowserProfile("Chrome", "Default", CHROMIUM, directory / "History")
        rows = read_history(profile)
        self.assertEqual(len(rows), 4)
        by_url = {url: (date, count) for url, date, count in rows}
        self.assertEqual(by_url["https://charter97.org/ru/news/2026/3/12/"], ("2026-03-12", 47))

    def test_firefox_history_and_dates(self):
        directory = build_firefox_profile(self.root / "f", HISTORY)
        profile = BrowserProfile("Firefox", "d", FIREFOX, directory / "places.sqlite")
        rows = read_history(profile)
        by_url = {url: (date, count) for url, date, count in rows}
        self.assertEqual(by_url["https://charter97.org/ru/news/2026/3/12/"], ("2026-03-12", 47))

    def test_chromium_bookmarks_are_read_from_nested_folders(self):
        directory = build_chromium_profile(
            self.root / "p", HISTORY, ["https://charter97.org/"]
        )
        profile = BrowserProfile(
            "Chrome", "Default", CHROMIUM, directory / "History", directory / "Bookmarks"
        )
        self.assertEqual(read_bookmarks(profile), ["https://charter97.org/"])

    def test_firefox_bookmarks_are_read(self):
        directory = build_firefox_profile(
            self.root / "f", HISTORY, ["https://charter97.org/"]
        )
        profile = BrowserProfile("Firefox", "d", FIREFOX, directory / "places.sqlite")
        self.assertEqual(read_bookmarks(profile), ["https://charter97.org/"])

    def test_corrupt_database_does_not_crash_the_check(self):
        """Один испорченный профиль не должен срывать проверку остальных."""
        directory = self.root / "broken"
        directory.mkdir(parents=True)
        (directory / "History").write_bytes("это не база данных".encode("utf-8"))
        profile = BrowserProfile("Chrome", "Default", CHROMIUM, directory / "History")
        self.assertEqual(read_history(profile), [])


class TestPrivacy(unittest.TestCase):
    def test_temporary_copy_is_removed(self):
        """Копия истории не должна оставаться на диске после чтения."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = build_chromium_profile(Path(tmp) / "p", HISTORY)
            profile = BrowserProfile("Chrome", "Default", CHROMIUM, directory / "History")
            before = set(Path(tempfile.gettempdir()).glob("browser-read-*"))
            read_history(profile)
            after = set(Path(tempfile.gettempdir()).glob("browser-read-*"))
            self.assertEqual(before, after)

    def test_temporary_copy_is_removed_even_after_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.__class__._broken(Path(tmp))
            profile = BrowserProfile("Chrome", "Default", CHROMIUM, directory / "History")
            before = set(Path(tempfile.gettempdir()).glob("browser-read-*"))
            read_history(profile)
            after = set(Path(tempfile.gettempdir()).glob("browser-read-*"))
            self.assertEqual(before, after)

    @staticmethod
    def _broken(root: Path) -> Path:
        directory = root / "broken"
        directory.mkdir(parents=True)
        (directory / "History").write_bytes("мусор".encode("utf-8"))
        return directory


class TestObservations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        directory = build_chromium_profile(
            Path(self.tmp.name) / "p", HISTORY, ["https://t.me/badchannel"]
        )
        self.profile = BrowserProfile(
            "Chrome", "Default", CHROMIUM, directory / "History", directory / "Bookmarks"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_repeated_visits_to_one_site_collapse_into_one_observation(self):
        """Иначе один сайт занял бы в отчёте десятки строк."""
        visits = [o for o in collect(self.profile) if o.value == "charter97.org"]
        self.assertEqual(len(visits), 1)

    def test_visit_counts_are_summed_and_latest_date_kept(self):
        visit = next(o for o in collect(self.profile) if o.value == "charter97.org")
        self.assertIn("посещений: 50", visit.source)  # 47 + 3
        self.assertEqual(visit.occurred_at, "2026-03-12")

    def test_social_links_in_history_are_recognised(self):
        """История ловит не только сайты, но и ссылки на соцсети."""
        saved = [o for o in collect(self.profile) if o.platform == "telegram"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].trace_type, "saved")
        self.assertEqual(saved[0].value, "badchannel")

    def test_search_engine_pages_do_not_become_findings(self):
        values = {o.value for o in collect(self.profile)}
        self.assertIn("charter97.org", values)
        self.assertNotIn("apps.apple.com", values)


class TestEndToEnd(unittest.TestCase):
    """Полный путь: история браузера -> сопоставление -> совпадение."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        directory = build_chromium_profile(
            Path(self.tmp.name) / "p", HISTORY, ["https://t.me/badchannel"]
        )
        self.profile = BrowserProfile(
            "Chrome", "Default", CHROMIUM, directory / "History", directory / "Bookmarks"
        )
        self.engine = MatchEngine(
            build_index(
                "Интернет-ресурс charter97.org",
                "Telegram-канал https://t.me/badchannel",
            )
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_registry_site_visited_by_the_user_is_found(self):
        matches = self.engine.match_all(collect_all([self.profile]))
        found = {(m.observation.value, m.confidence) for m in matches}
        self.assertIn(("charter97.org", EXACT), found)

    def test_bookmarked_channel_is_found_as_saved(self):
        matches = self.engine.match_all(collect_all([self.profile]))
        telegram = [m for m in matches if m.observation.platform == "telegram"]
        self.assertEqual(len(telegram), 1)
        self.assertEqual(telegram[0].observation.trace_type, "saved")

    def test_ordinary_browsing_produces_no_matches(self):
        matches = self.engine.match_all(collect_all([self.profile]))
        values = {m.observation.value for m in matches}
        self.assertNotIn("example.com", values)
        self.assertNotIn("google.com", values)


if __name__ == "__main__":
    unittest.main()
