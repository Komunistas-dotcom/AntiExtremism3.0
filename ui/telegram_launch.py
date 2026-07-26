"""Запуск проверки Telegram в отдельном консольном окне.

Почему не в этом же окне
-------------------------
`connectors/telegram.py` и `connectors/check_telegram.py` — завершённый,
покрытый тестами блок (см. CLAUDE.md, раздел «Not to touch»): вход в
аккаунт там намеренно построен на обычном консольном вводе (номер, код,
облачный пароль), и трогать эту логику без явного запроса не нужно.

Поэтому мастер не встраивает форму входа в свои окна, а запускает готовый
`python -m connectors.check_telegram` отдельным процессом в новой консоли.
Ключ приложения передаётся через переменные среды, которые
`check_telegram.py` и так умеет читать (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`)
— это не требует изменений в telegram-модулях.

Следствие: отчёт по Telegram печатается в том отдельном окне и не попадает
в общий отчёт мастера. Это сознательный компромисс ради того, чтобы не
трогать протестированный код входа.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# На Windows это открывает настоящее отдельное окно консоли.
# На других платформах атрибута нет — используем 0 (обычный подпроцесс),
# т.к. само приложение ориентировано на Windows (см. CLAUDE.md §1).
_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


class LaunchError(RuntimeError):
    """Не удалось запустить проверку Telegram."""


def launch(registry_path: Path, api_id: str, api_hash: str, extra_args: list[str] | None = None):
    """Запустить `connectors.check_telegram` в новом окне консоли.

    Возвращает объект процесса; ждать его завершения не нужно — окно
    консоли живёт независимо от мастера.
    """
    api_id = api_id.strip()
    api_hash = api_hash.strip()
    if not api_id.isdigit():
        raise LaunchError("Ключ приложения (api_id) должен быть числом.")
    if not api_hash:
        raise LaunchError("Не указан api_hash.")

    env = os.environ.copy()
    env["TELEGRAM_API_ID"] = api_id
    env["TELEGRAM_API_HASH"] = api_hash

    command = [sys.executable, "-m", "connectors.check_telegram", str(registry_path)]
    command.extend(extra_args or [])

    try:
        return subprocess.Popen(
            command, env=env, cwd=REPO_ROOT, creationflags=_NEW_CONSOLE
        )
    except OSError as exc:
        raise LaunchError(f"Не удалось запустить проверку: {exc}") from exc
