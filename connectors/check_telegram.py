"""Проверка Telegram через защищённый вход.

Запуск::

    python -m connectors.check_telegram data/registry.db

Нужен ключ приложения Telegram. Его выдаёт сам Telegram на странице
https://my.telegram.org → API development tools. Передаётся через
переменные среды ``TELEGRAM_API_ID`` и ``TELEGRAM_API_HASH`` либо
ключами командной строки.

Что делает программа:

1. просит номер телефона и код, который придёт в Telegram;
2. читает подписки, а затем — пересылки и ссылки в своих сообщениях;
3. сверяет найденное с реестром и показывает отчёт;
4. выходит из аккаунта, чтобы в разделе «Устройства» не осталось следа.

Ни переписка, ни файл сессии на диск не записываются.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from matching.engine import MatchEngine, RegistryIndex
from matching.report import WARNING, render, save

from .telegram import TELETHON_MISSING, TelegramCheckSettings, collect

CONSENT = """\
Программа сейчас войдёт в ваш аккаунт Telegram, чтобы проверить подписки,
пересылки и ссылки в ваших сообщениях.

Важно понимать:
  • вход настоящий: понадобится номер, код и, если включён, облачный пароль;
  • переписка НЕ сохраняется на диск — сообщения проверяются в памяти;
  • по окончании программа выйдет из аккаунта и удалит сеанс;
  • Telegram может временно ограничить аккаунт за необычную нагрузку;
    чтение идёт неспешно, чтобы этого избежать.

Продолжить? [да/нет]: """


async def run_check(snapshot: Path, settings: TelegramCheckSettings, phone: str | None):
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    index = RegistryIndex(snapshot)
    engine = MatchEngine(index)

    # Пустая строковая сессия = сессия живёт только в памяти,
    # на диск не попадает ничего.
    client = TelegramClient(
        StringSession(), settings.api_id, settings.api_hash, device_model="Self-Audit"
    )

    await client.connect()
    try:
        if not await client.is_user_authorized():
            number = phone or input("Номер телефона (в формате +375…): ").strip()
            await client.send_code_request(number)
            code = input("Код из Telegram: ").strip()
            try:
                await client.sign_in(phone=number, code=code)
            except SessionPasswordNeededError:
                password = getpass.getpass("Облачный пароль (не сохраняется): ")
                await client.sign_in(password=password)

        print("\nВход выполнен. Идёт проверка…\n")

        observations = []
        async for observation in collect(client, settings):
            observations.append(observation)

        matches = engine.match_all(observations)
        print()
        print(f"Проверено следов: {len(observations)}")
        print()
        return render(matches, index.meta), settings.errors
    finally:
        if settings.logout_when_done:
            try:
                await client.log_out()
                print("Сеанс Telegram завершён, в «Устройствах» следа не осталось.")
            except Exception:
                print(
                    "Не удалось автоматически выйти из аккаунта. "
                    "Завершите сеанс вручную: Telegram → Настройки → Устройства.",
                    file=sys.stderr,
                )
        else:
            await client.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="connectors.check_telegram",
        description="Проверить Telegram через защищённый вход (MTProto).",
    )
    parser.add_argument("snapshot", type=Path, help="файл базы реестра (registry.db)")
    parser.add_argument("--api-id", type=int, default=os.environ.get("TELEGRAM_API_ID"))
    parser.add_argument("--api-hash", default=os.environ.get("TELEGRAM_API_HASH"))
    parser.add_argument("--phone", help="номер телефона, чтобы не вводить вручную")
    parser.add_argument(
        "--subscriptions-only",
        action="store_true",
        help="не читать сообщения вообще, взять только список подписок",
    )
    parser.add_argument(
        "--all-messages",
        action="store_true",
        help="читать не только свои сообщения, но и чужие в диалогах",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3000,
        help="сколько сообщений смотреть в одном диалоге (по умолчанию 3000)",
    )
    parser.add_argument(
        "--keep-session",
        action="store_true",
        help="не выходить из аккаунта после проверки",
    )
    parser.add_argument("--save", type=Path, metavar="ФАЙЛ", help="сохранить отчёт")
    parser.add_argument("--yes", action="store_true", help="не спрашивать согласия")
    args = parser.parse_args(argv)

    try:
        from .telegram import require_telethon

        require_telethon()
    except RuntimeError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    if not args.api_id or not args.api_hash:
        print(
            "Ошибка: не задан ключ приложения Telegram.\n"
            "Получите его на https://my.telegram.org (API development tools)\n"
            "и передайте через TELEGRAM_API_ID и TELEGRAM_API_HASH "
            "либо ключами --api-id и --api-hash.",
            file=sys.stderr,
        )
        return 2

    if not args.snapshot.is_file():
        print(f"Ошибка: файл базы реестра не найден: {args.snapshot}", file=sys.stderr)
        return 2

    if not args.yes and input(CONSENT).strip().lower() not in {"да", "y", "yes"}:
        print("Проверка отменена.")
        return 1

    settings = TelegramCheckSettings(
        api_id=int(args.api_id),
        api_hash=args.api_hash,
        message_limit=args.limit,
        read_messages=not args.subscriptions_only,
        own_messages_only=not args.all_messages,
        logout_when_done=not args.keep_session,
        progress=lambda text: print(text),
    )

    try:
        report, errors = asyncio.run(run_check(args.snapshot, settings, args.phone))
    except KeyboardInterrupt:
        print("\nПроверка прервана.")
        return 1
    except Exception as exc:
        print(f"Ошибка при работе с Telegram: {exc}", file=sys.stderr)
        return 3

    print(report)
    if errors:
        print()
        print("Не удалось прочитать некоторые диалоги (это нормально):")
        for line in errors[:10]:
            print(f"  • {line}")

    if args.save:
        print()
        print(WARNING)
        print(f"Отчёт сохранён: {save(report, args.save).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
