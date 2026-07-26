"""Мастер: выбор базы реестра → выбор источников → проверка → отчёт.

Запуск::

    python -m ui

Построен на Tkinter (стандартная библиотека), без сторонних зависимостей —
см. CLAUDE.md §0/§5 про упаковку в `.exe` силами пользователя.

Устройство
----------
Одно окно, внутри которого по очереди показываются четыре страницы
(`tk.Frame`): выбор/сборка базы реестра → выбор источников → ход проверки →
отчёт. Долгие операции (сборка базы, чтение браузеров) выполняются в
фоновом потоке и сообщают о ходе через `queue.Queue`, чтобы не подвешивать
окно — сам Tkinter трогать можно только из главного потока.

Источники, реализованные к этому моменту:
* **История браузера** — выполняется прямо здесь, результат идёт в общий отчёт.
* **Telegram** — запускается отдельным консольным окном
  (`ui/telegram_launch.py`), чтобы не трогать протестированный вход по
  MTProto в `connectors/telegram.py` (см. CLAUDE.md, «Not to touch»).
  Отчёт по нему печатается в том отдельном окне и в общий не попадает —
  это осознанный компромисс, а не недосмотр.

Остальные источники (Meta, TikTok, YouTube, VK, OK, X, файлы) в реестре
ещё не реализованы — на странице выбора они показаны как недоступные.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from matching.engine import MatchEngine, RegistryIndex
from matching.report import WARNING, render, save

from . import browser_runner, registry_state, telegram_launch

WINDOW_TITLE = "Самопроверка на экстремистские материалы"

# Источники, которых пока нет — показываются в списке как недоступные,
# чтобы человек видел общую картину и не думал, что про них забыли.
PLANNED_SOURCES = (
    "Instagram + Facebook (Meta)",
    "TikTok",
    "YouTube",
    "ВКонтакте",
    "Одноклассники",
    "X (Twitter)",
    "Книги и музыка",
)


class WizardApp(tk.Tk):
    """Главное окно мастера."""

    def __init__(self) -> None:
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry("760x560")
        self.minsize(640, 480)

        # Общее состояние, доступное всем страницам.
        self.registry_index: RegistryIndex | None = None
        self.registry_summary: registry_state.RegistrySummary | None = None
        self.last_matches: list = []

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        self._container = container

        self.frames: dict[str, ttk.Frame] = {}
        for cls in (RegistryPage, SourcesPage, ProgressPage, ReportPage):
            frame = cls(container, self)
            self.frames[cls.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.show("RegistryPage")

    def show(self, name: str) -> None:
        frame = self.frames[name]
        if hasattr(frame, "on_show"):
            frame.on_show()
        frame.tkraise()


# --------------------------------------------------------------------------
# Страница 1 — база реестра
# --------------------------------------------------------------------------


class RegistryPage(ttk.Frame):
    """Найти или собрать базу реестра, показать её дату и объём."""

    def __init__(self, parent: tk.Widget, app: WizardApp) -> None:
        super().__init__(parent, padding=24)
        self.app = app

        ttk.Label(
            self, text="База реестра", font=("TkDefaultFont", 14, "bold")
        ).pack(anchor="w")
        ttk.Label(
            self,
            wraplength=680,
            justify="left",
            text=(
                "Программа сверяет ваши данные с локальным файлом базы, "
                "собранным из республиканского списка экстремистских "
                "материалов. Ничего из ваших данных никуда не отправляется."
            ),
        ).pack(anchor="w", pady=(4, 16))

        self.status_var = tk.StringVar(value="Поиск базы…")
        ttk.Label(self, textvariable=self.status_var, wraplength=680, justify="left").pack(
            anchor="w", pady=(0, 16)
        )

        buttons = ttk.Frame(self)
        buttons.pack(anchor="w", pady=(0, 16))
        ttk.Button(
            buttons, text="Указать файл реестра или базу…", command=self._choose_file
        ).pack(side="left")

        self.next_button = ttk.Button(
            self, text="Далее →", command=lambda: self.app.show("SourcesPage")
        )
        self.next_button.pack(anchor="e", side="bottom")
        self.next_button.state(["disabled"])

        self._shown_once = False

    def on_show(self) -> None:
        if self._shown_once:
            return
        self._shown_once = True
        self._try_load_default()

    def _try_load_default(self) -> None:
        default_db = registry_state.default_registry_path()
        if default_db.is_file():
            self._load_index(default_db)
            return

        source = registry_state.find_default_source()
        if source is not None:
            self.status_var.set(
                f"Готовой базы нет, но найден исходный файл: {source.name}. Собираю базу…"
            )
            self._build_and_load(source, default_db)
            return

        self.status_var.set(
            "Готовая база не найдена. Нажмите «Указать файл реестра или базу…» "
            "и выберите файл со списком (.doc с сайта министерства) "
            "либо уже готовый registry.db."
        )

    def _choose_file(self) -> None:
        path_str = filedialog.askopenfilename(
            title="Файл реестра",
            filetypes=[
                ("Реестр или база", "*.doc *.docx *.odt *.rtf *.html *.htm *.md *.db"),
                ("Все файлы", "*.*"),
            ],
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() == ".db":
            self._load_index(path)
        else:
            destination = registry_state.default_registry_path()
            self._build_and_load(path, destination)

    def _build_and_load(self, source: Path, destination: Path) -> None:
        self.next_button.state(["disabled"])

        def worker() -> None:
            try:
                registry_state.build_snapshot(
                    source, destination, progress=lambda t: self.after(0, self.status_var.set, t)
                )
            except Exception as exc:  # noqa: BLE001 — показываем причину человеку
                self.after(0, self._on_error, str(exc))
                return
            self.after(0, self._load_index, destination)

        threading.Thread(target=worker, daemon=True).start()

    def _on_error(self, message: str) -> None:
        self.status_var.set(f"Не удалось собрать базу: {message}")

    def _load_index(self, path: Path) -> None:
        try:
            index = RegistryIndex(path)
        except Exception as exc:  # noqa: BLE001
            self._on_error(str(exc))
            return

        self.app.registry_index = index
        summary = registry_state.summarize_meta(path, index.meta)
        self.app.registry_summary = summary

        age = f" ({summary.age_label})" if summary.age_label else ""
        self.status_var.set(
            f"База загружена: {path.name}\n"
            f"Записей в реестре: {summary.entry_count}, "
            f"признаков для сверки: {summary.identifier_count}\n"
            f"Собрана: {summary.built_at}{age}"
        )
        self.next_button.state(["!disabled"])


# --------------------------------------------------------------------------
# Страница 2 — выбор источников
# --------------------------------------------------------------------------


class SourcesPage(ttk.Frame):
    """Выбор, что проверять в этом окне, плюс отдельный запуск Telegram."""

    def __init__(self, parent: tk.Widget, app: WizardApp) -> None:
        super().__init__(parent, padding=24)
        self.app = app

        ttk.Label(
            self, text="Что проверяем", font=("TkDefaultFont", 14, "bold")
        ).pack(anchor="w")

        ttk.Label(
            self,
            wraplength=680,
            justify="left",
            text="Отметьте источники, которые нужно проверить в этом окне.",
        ).pack(anchor="w", pady=(4, 12))

        # ttk.Checkbutton не умеет переносить текст по словам (нет опции
        # wraplength), поэтому длинное описание вынесено в отдельную подпись.
        self.browser_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self, text="История и закладки браузера", variable=self.browser_var
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            self,
            wraplength=680,
            justify="left",
            foreground="#555555",
            text=(
                "Chrome, Edge, Яндекс.Браузер, Opera, Brave, Vivaldi, Firefox — "
                "не требует входа никуда."
            ),
        ).pack(anchor="w", padx=(20, 0), pady=(0, 2))

        ttk.Separator(self).pack(fill="x", pady=12)

        ttk.Label(
            self,
            text="Telegram проверяется в отдельном окне",
            font=("TkDefaultFont", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            self,
            wraplength=680,
            justify="left",
            text=(
                "Вход в Telegram требует номер телефона и код подтверждения, "
                "поэтому он открывается отдельным консольным окном — там же "
                "будет и его отчёт. Общего отчёта с этим окном он не разделяет."
            ),
        ).pack(anchor="w", pady=(2, 8))
        ttk.Button(
            self, text="Открыть проверку Telegram…", command=self._open_telegram_dialog
        ).pack(anchor="w")

        ttk.Separator(self).pack(fill="x", pady=12)

        ttk.Label(self, text="Пока не реализовано:").pack(anchor="w")
        planned = ttk.Frame(self)
        planned.pack(anchor="w", pady=(2, 0))
        for name in PLANNED_SOURCES:
            ttk.Checkbutton(planned, text=name, state="disabled").pack(anchor="w")

        nav = ttk.Frame(self)
        nav.pack(side="bottom", fill="x", pady=(16, 0))
        ttk.Button(
            nav, text="← Назад", command=lambda: self.app.show("RegistryPage")
        ).pack(side="left")
        ttk.Button(nav, text="Начать проверку →", command=self._start).pack(side="right")

    def on_show(self) -> None:
        pass

    def _start(self) -> None:
        if not self.browser_var.get():
            messagebox.showinfo(
                WINDOW_TITLE,
                "Отметьте хотя бы один источник для проверки в этом окне, "
                "либо воспользуйтесь отдельной проверкой Telegram.",
            )
            return
        self.app.show("ProgressPage")

    def _open_telegram_dialog(self) -> None:
        TelegramLaunchDialog(self, self.app)


class TelegramLaunchDialog(tk.Toplevel):
    """Небольшое окно для ввода ключа приложения перед запуском."""

    def __init__(self, parent: tk.Widget, app: WizardApp) -> None:
        super().__init__(parent)
        self.app = app
        self.title("Проверка Telegram")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        pad = {"padx": 12, "pady": 6}

        ttk.Label(
            self,
            wraplength=380,
            justify="left",
            text=(
                "Ключ приложения выдаёт сам Telegram на странице "
                "my.telegram.org → API development tools. Он нужен один раз "
                "и на диск не сохраняется."
            ),
        ).grid(row=0, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(self, text="api_id:").grid(row=1, column=0, sticky="e", **pad)
        self.api_id_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.api_id_var, width=30).grid(
            row=1, column=1, sticky="w", **pad
        )

        ttk.Label(self, text="api_hash:").grid(row=2, column=0, sticky="e", **pad)
        self.api_hash_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.api_hash_var, width=30, show="•").grid(
            row=2, column=1, sticky="w", **pad
        )

        buttons = ttk.Frame(self)
        buttons.grid(row=3, column=0, columnspan=2, pady=(6, 12))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(buttons, text="Открыть окно проверки", command=self._launch).pack(
            side="left", padx=6
        )

    def _launch(self) -> None:
        if self.app.registry_index is None:
            messagebox.showerror(WINDOW_TITLE, "Сначала загрузите базу реестра.")
            return
        try:
            telegram_launch.launch(
                self.app.registry_summary.path,
                self.api_id_var.get(),
                self.api_hash_var.get(),
            )
        except telegram_launch.LaunchError as exc:
            messagebox.showerror(WINDOW_TITLE, str(exc))
            return
        messagebox.showinfo(
            WINDOW_TITLE,
            "Открывается отдельное окно консоли — следуйте инструкциям в нём. "
            "Отчёт по Telegram появится там же.",
        )
        self.destroy()


# --------------------------------------------------------------------------
# Страница 3 — ход проверки
# --------------------------------------------------------------------------


class ProgressPage(ttk.Frame):
    """Фоновый запуск выбранных источников с журналом хода проверки."""

    def __init__(self, parent: tk.Widget, app: WizardApp) -> None:
        super().__init__(parent, padding=24)
        self.app = app
        self._queue: queue.Queue = queue.Queue()

        ttk.Label(
            self, text="Идёт проверка…", font=("TkDefaultFont", 14, "bold")
        ).pack(anchor="w")

        self.log = tk.Text(self, height=18, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(12, 12))

        self.back_button = ttk.Button(
            self, text="← Назад", command=lambda: self.app.show("SourcesPage")
        )
        self.back_button.pack(side="left")

    def on_show(self) -> None:
        sources_page: SourcesPage = self.app.frames["SourcesPage"]
        self._clear_log()
        self.back_button.state(["disabled"])
        self._log("Проверка начата.")

        include_browser = sources_page.browser_var.get()
        index = self.app.registry_index

        def worker() -> None:
            observations = []
            if include_browser:
                observations.extend(
                    browser_runner.run(progress=lambda t: self._queue.put(("log", t)))
                )
            self._queue.put(("observations", observations))

            engine = MatchEngine(index)
            matches = engine.match_all(observations)
            self._queue.put(("done", matches))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll)

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    self._log("Готово.")
                    self.back_button.state(["!disabled"])
                    self.app.last_matches = payload
                    report_page: ReportPage = self.app.frames["ReportPage"]
                    report_page.set_matches(payload)
                    self.app.show("ReportPage")
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll)


# --------------------------------------------------------------------------
# Страница 4 — отчёт
# --------------------------------------------------------------------------


class ReportPage(ttk.Frame):
    """Показ отчёта. Сохранение на диск — только по явному действию."""

    def __init__(self, parent: tk.Widget, app: WizardApp) -> None:
        super().__init__(parent, padding=24)
        self.app = app
        self._report_text = ""

        ttk.Label(self, text="Отчёт", font=("TkDefaultFont", 14, "bold")).pack(anchor="w")

        self.text = tk.Text(self, wrap="word", state="disabled")
        self.text.pack(fill="both", expand=True, pady=(12, 12))

        nav = ttk.Frame(self)
        nav.pack(side="bottom", fill="x")
        ttk.Button(
            nav, text="← Начать заново", command=lambda: self.app.show("SourcesPage")
        ).pack(side="left")
        ttk.Button(nav, text="Сохранить в файл…", command=self._save).pack(side="right")

    def set_matches(self, matches: list) -> None:
        meta = self.app.registry_index.meta if self.app.registry_index else None
        self._report_text = render(matches, meta)
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", self._report_text)
        self.text.configure(state="disabled")

    def _save(self) -> None:
        proceed = messagebox.askyesno(WINDOW_TITLE, WARNING + "\n\nСохранить отчёт в файл?")
        if not proceed:
            return
        path_str = filedialog.asksaveasfilename(
            title="Сохранить отчёт",
            defaultextension=".txt",
            filetypes=[("Текстовый файл", "*.txt"), ("Все файлы", "*.*")],
        )
        if not path_str:
            return
        saved = save(self._report_text, Path(path_str))
        messagebox.showinfo(WINDOW_TITLE, f"Отчёт сохранён: {saved}")


def main() -> int:
    app = WizardApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
