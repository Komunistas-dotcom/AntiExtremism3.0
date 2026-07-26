"""Проверки вспомогательных модулей интерфейса — без Tkinter.

Эти модули (`ui.registry_state`, `ui.browser_runner`, `ui.telegram_launch`)
намеренно не содержат виджетов: вся логика, которую можно тестировать
без графики, вынесена сюда, а сам `ui.app` — это только сборка виджетов
вокруг них (см. docstring `ui/app.py`).
"""

import tempfile
import unittest
from pathlib import Path

from tests.make_docx import build_docx
from ui import registry_state, telegram_launch


class TestDefaultPaths(unittest.TestCase):
    def test_default_registry_path_is_under_data(self):
        path = registry_state.default_registry_path(root=Path("/somewhere"))
        self.assertEqual(path, Path("/somewhere/data/registry.db"))

    def test_find_default_source_picks_up_a_doc_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            source = build_docx(
                root / "data" / "spisok.docx",
                [["Инф.", "https://t.me/x", "Решение суда"]],
                {},
            )
            found = registry_state.find_default_source(root=root)
            self.assertEqual(found, source)

    def test_find_default_source_is_none_without_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(registry_state.find_default_source(root=Path(tmp)))


class TestBuildSnapshot(unittest.TestCase):
    def test_builds_a_working_snapshot_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = build_docx(
                root / "spisok.docx",
                [["Инф.", "https://t.me/badchannel", "Решение суда от 1 января 2021"]],
                {},
            )
            destination = root / "out" / "registry.db"
            messages: list[str] = []

            result = registry_state.build_snapshot(
                source, destination, progress=messages.append
            )

            self.assertTrue(destination.is_file())
            self.assertEqual(result["entries"], 1)
            self.assertTrue(messages, "прогресс должен был сообщаться")
            self.assertIn("собрана", messages[-1].lower())

    def test_raises_a_readable_error_for_an_empty_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = build_docx(root / "empty.docx", [], {})
            with self.assertRaises(RuntimeError):
                registry_state.build_snapshot(source, root / "out.db")


class TestSummarizeMeta(unittest.TestCase):
    def test_formats_entry_and_identifier_counts(self):
        summary = registry_state.summarize_meta(
            Path("registry.db"),
            {
                "entry_count": "5861",
                "identifier_count": "10728",
                "built_at": "2020-01-01T00:00:00+00:00",
            },
        )
        self.assertEqual(summary.entry_count, 5861)
        self.assertEqual(summary.identifier_count, 10728)
        self.assertIn("дн. назад", summary.age_label)

    def test_survives_missing_or_malformed_meta(self):
        """Битые метаданные не должны валить интерфейс — только показ даты."""
        summary = registry_state.summarize_meta(Path("registry.db"), {})
        self.assertEqual(summary.entry_count, 0)
        self.assertEqual(summary.age_label, "")


class TestTelegramLaunch(unittest.TestCase):
    def test_rejects_non_numeric_api_id(self):
        with self.assertRaises(telegram_launch.LaunchError):
            telegram_launch.launch(Path("registry.db"), "не число", "hash")

    def test_rejects_empty_api_hash(self):
        with self.assertRaises(telegram_launch.LaunchError):
            telegram_launch.launch(Path("registry.db"), "12345", "  ")

    def test_valid_credentials_are_accepted_without_raising(self):
        # Сам процесс запускать не даём (нет python -m connectors.check_telegram
        # в изолированном тесте) — подменяем Popen, чтобы проверить только
        # проверку входных данных и переменные среды.
        calls = []

        def fake_popen(command, env, cwd, creationflags):
            calls.append((command, env, cwd, creationflags))

            class _Proc:
                pass

            return _Proc()

        original = telegram_launch.subprocess.Popen
        telegram_launch.subprocess.Popen = fake_popen
        try:
            telegram_launch.launch(Path("registry.db"), "12345", "abcdef")
        finally:
            telegram_launch.subprocess.Popen = original

        self.assertEqual(len(calls), 1)
        command, env, _cwd, _flags = calls[0]
        self.assertIn("connectors.check_telegram", command)
        self.assertEqual(env["TELEGRAM_API_ID"], "12345")
        self.assertEqual(env["TELEGRAM_API_HASH"], "abcdef")


if __name__ == "__main__":
    unittest.main()
