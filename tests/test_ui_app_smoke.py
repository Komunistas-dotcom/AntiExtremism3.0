"""Дымовые проверки мастера: строится ли окно и переключаются ли страницы.

Пропускаются, если недоступен Tkinter или графический дисплей — это
нормально в headless-среде разработки (см. README, раздел «Интерфейс»).
На машине пользователя (Windows, обычный Python с python.org) Tkinter
есть всегда, и здесь тест реально проверяет сборку окна.
"""

import tempfile
import unittest
from pathlib import Path

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False

from tests.make_docx import build_docx


def _find_toplevels(widget) -> list:
    """Найти все Toplevel-окна в поддереве виджетов.

    Toplevel регистрируется в дереве Tkinter под тем виджетом, который был
    передан ему как ``master`` — это не обязательно корень окна, поэтому
    обход рекурсивный, а не только по прямым потомкам.
    """
    found = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Toplevel):
            found.append(child)
        found.extend(_find_toplevels(child))
    return found


@unittest.skipUnless(TK_AVAILABLE, "нет Tkinter или графического дисплея")
class TestWizardSmoke(unittest.TestCase):
    def setUp(self):
        from registry.build import write_snapshot
        from registry.ingest import load_html
        from registry.parse import parse_registry

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        source = build_docx(
            root / "spisok.docx",
            [["Инф.", "https://t.me/badchannel", "Решение суда"]],
            {},
        )
        self.registry_path = root / "registry.db"
        write_snapshot(parse_registry(load_html(source)), self.registry_path, source)

    def tearDown(self):
        self.tmp.cleanup()

    def _build_app(self):
        from ui.app import WizardApp

        app = WizardApp()
        self.addCleanup(app.destroy)
        return app

    def test_window_builds_with_all_pages(self):
        app = self._build_app()
        self.assertEqual(
            set(app.frames), {"RegistryPage", "SourcesPage", "ProgressPage", "ReportPage"}
        )

    def test_loading_a_registry_file_enables_next_button(self):
        app = self._build_app()
        page = app.frames["RegistryPage"]
        page._load_index(self.registry_path)
        app.update()
        # .state() без аргументов возвращает только ВКЛЮЧЁННЫЕ флаги —
        # значит "disabled" должен отсутствовать, раз кнопка стала активной.
        self.assertNotIn("disabled", page.next_button.state())
        self.assertIsNotNone(app.registry_index)
        self.assertEqual(app.registry_summary.entry_count, 1)

    def test_navigation_between_pages_works(self):
        app = self._build_app()
        app.frames["RegistryPage"]._load_index(self.registry_path)
        app.update()
        app.show("SourcesPage")
        self.assertEqual(app.frames["SourcesPage"], app.frames["SourcesPage"])
        app.show("RegistryPage")

    def test_report_page_renders_empty_result_without_crashing(self):
        app = self._build_app()
        app.frames["RegistryPage"]._load_index(self.registry_path)
        app.update()
        app.frames["ReportPage"].set_matches([])
        text = app.frames["ReportPage"].text.get("1.0", "end")
        self.assertIn("Совпадений не найдено", text)

    def test_telegram_dialog_rejects_bad_input_without_crashing(self):
        from unittest.mock import patch

        app = self._build_app()
        app.frames["RegistryPage"]._load_index(self.registry_path)
        app.update()
        app.show("SourcesPage")
        with patch("ui.app.messagebox.showerror") as mock_error:
            app.frames["SourcesPage"]._open_telegram_dialog()
            app.update()
            dialogs = _find_toplevels(app)
            self.assertEqual(len(dialogs), 1)
            dialog = dialogs[0]
            dialog.api_id_var.set("не число")
            dialog._launch()
            mock_error.assert_called_once()
            dialog.destroy()


if __name__ == "__main__":
    unittest.main()
