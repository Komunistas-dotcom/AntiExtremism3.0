"""Проверки движка сопоставления."""

import tempfile
import unittest
from pathlib import Path

from matching.engine import EXACT, PROBABLE, MatchEngine, RegistryIndex
from matching.observation import (
    observe_handle,
    observe_numeric_id,
    observe_url,
    observe_visited_domain,
)
from matching.report import render, summarize
from registry.build import write_snapshot
from registry.ingest import load_html
from registry.parse import parse_registry

HEADER = "<tr><td>Вид экстремистских материалов</td><td>Н</td><td>Наименование суда</td></tr>"


def build_index(*descriptions: str) -> RegistryIndex:
    rows = "".join(
        f"<tr><td>Информационная продукция</td><td>{d}</td>"
        f"<td>Решение суда города N от {i + 1} января 2021 года</td></tr>"
        for i, d in enumerate(descriptions)
    )
    html = f"<html><body><table>{HEADER}{rows}</table></body></html>"
    tmp = tempfile.mkdtemp()
    source = Path(tmp) / "registry.html"
    source.write_text(html, encoding="utf-8")
    destination = Path(tmp) / "registry.db"
    write_snapshot(parse_registry(load_html(source)), destination, source)
    return RegistryIndex(destination)


class TestExactMatching(unittest.TestCase):
    def setUp(self):
        self.engine = MatchEngine(
            build_index(
                "Telegram-канал, идентификатор https://t.me/badchannel и идентификатор "
                "(ID), содержащий последовательность цифр 1442177936",
                "Сообщество https://vk.com/badgroup",
            )
        )

    def test_subscription_by_handle_is_found(self):
        observation = observe_handle("telegram", "@badchannel", "subscription", "Telegram/чаты")
        matches = self.engine.match_one(observation)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].confidence, EXACT)

    def test_link_in_a_message_is_found(self):
        observations = observe_url("https://t.me/badchannel", "link", "Telegram/переписка")
        matches = self.engine.match_all(observations)
        self.assertEqual(matches[0].confidence, EXACT)

    def test_preview_link_matches_the_same_channel(self):
        """t.me/s/... в истории браузера — тот же канал."""
        observations = observe_url("https://t.me/s/badchannel", "visit", "Chrome")
        self.assertTrue(self.engine.match_all(observations))

    def test_numeric_id_matches_where_it_is_available(self):
        observation = observe_numeric_id("telegram", 1442177936, "subscription", "TDLib")
        matches = self.engine.match_one(observation)
        self.assertEqual(matches[0].confidence, EXACT)

    def test_clean_account_produces_nothing(self):
        observation = observe_handle("telegram", "@ordinary_channel", "subscription", "Telegram")
        self.assertEqual(self.engine.match_one(observation), [])

    def test_homoglyph_in_user_data_still_matches(self):
        """Ссылка с кириллической буквой у пользователя не должна теряться."""
        observations = observe_url("https://vk.соm/badgroup", "subscription", "VK")
        matches = self.engine.match_all(observations)
        self.assertEqual(matches[0].confidence, EXACT)


class TestProbableMatching(unittest.TestCase):
    def setUp(self):
        self.engine = MatchEngine(build_index("Аккаунт https://instagram.com/mirrorname"))

    def test_same_name_on_another_platform_is_probable_not_exact(self):
        observation = observe_handle("tiktok", "mirrorname", "subscription", "TikTok")
        matches = self.engine.match_one(observation)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].confidence, PROBABLE)

    def test_same_platform_and_name_is_exact(self):
        observation = observe_handle("instagram", "mirrorname", "subscription", "Instagram")
        self.assertEqual(self.engine.match_one(observation)[0].confidence, EXACT)

    def test_short_names_do_not_travel_between_platforms(self):
        """Короткое имя даёт случайные совпадения — их быть не должно."""
        engine = MatchEngine(build_index("Аккаунт https://instagram.com/abc"))
        observation = observe_handle("tiktok", "abc", "subscription", "TikTok")
        self.assertEqual(engine.match_one(observation), [])

    def test_numeric_ids_never_travel_between_platforms(self):
        """Два одинаковых числа в разных сетях не значат ничего."""
        engine = MatchEngine(
            build_index("Сообщество https://vk.com/public28199915")
        )
        observation = observe_numeric_id("facebook", "28199915", "subscription", "Facebook")
        self.assertEqual(engine.match_one(observation), [])


class TestBrowserHistory(unittest.TestCase):
    def test_visited_site_is_matched(self):
        engine = MatchEngine(build_index("Интернет-ресурс charter97.org"))
        observations = observe_visited_domain("https://charter97.org/ru/news/1", "Chrome")
        matches = engine.match_all(observations)
        self.assertEqual(matches[0].confidence, EXACT)

    def test_ordinary_site_is_not_matched(self):
        engine = MatchEngine(build_index("Интернет-ресурс charter97.org"))
        observations = observe_visited_domain("https://example.com/page", "Chrome")
        self.assertEqual(engine.match_all(observations), [])


class TestCancelledAreExcluded(unittest.TestCase):
    def test_cancelled_entry_never_matches(self):
        engine = MatchEngine(build_index("Отменено."))
        observation = observe_handle("telegram", "anything", "subscription", "Telegram")
        self.assertEqual(engine.match_one(observation), [])


class TestReport(unittest.TestCase):
    def setUp(self):
        self.engine = MatchEngine(
            build_index("Telegram-канал https://t.me/badchannel и https://vk.com/badchannel")
        )

    def test_clean_report_says_so_and_states_its_limits(self):
        text = render([], {"built_at": "2026-07-25", "entry_count": "5861"})
        self.assertIn("Совпадений не найдено", text)
        self.assertIn("не проверялись", text)

    def test_report_separates_the_two_kinds_of_match(self):
        matches = self.engine.match_all(
            [
                observe_handle("telegram", "badchannel", "subscription", "Telegram/каналы"),
                observe_handle("tiktok", "badchannel", "like", "TikTok/лайки"),
            ]
        )
        text = render(matches)
        self.assertIn("ТОЧНЫЕ СОВПАДЕНИЯ", text)
        self.assertIn("ВЕРОЯТНЫЕ СОВПАДЕНИЯ", text)
        self.assertLess(text.index("ТОЧНЫЕ"), text.index("ВЕРОЯТНЫЕ"))

    def test_report_says_where_the_trace_was_found(self):
        matches = self.engine.match_all(
            [observe_handle("telegram", "badchannel", "subscription", "Telegram/каналы")]
        )
        text = render(matches)
        self.assertIn("подписка", text)
        self.assertIn("Telegram/каналы", text)

    def test_report_warns_before_saving(self):
        matches = self.engine.match_all(
            [observe_handle("telegram", "badchannel", "subscription", "Telegram")]
        )
        self.assertIn("улика", render(matches).lower() + "улика")
        self.assertIn("удалите", render(matches))

    def test_summary_counts_are_consistent(self):
        matches = self.engine.match_all(
            [
                observe_handle("telegram", "badchannel", "subscription", "Telegram"),
                observe_handle("tiktok", "badchannel", "like", "TikTok"),
            ]
        )
        stats = summarize(matches)
        self.assertEqual(stats["всего"], stats["точных"] + stats["вероятных"])


if __name__ == "__main__":
    unittest.main()
