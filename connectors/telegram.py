"""Проверка Telegram через защищённый вход (MTProto).

Почему не выгрузка Telegram Desktop
-----------------------------------
Выгрузка создаёт на диске архив со всей перепиской — то есть сама становится
уликой. Живой вход позволяет вообще ничего не сохранять: сообщения читаются
потоком в память, проверяются и забываются.

Что проверяется
---------------
* подписки на каналы, чаты и группы;
* репосты и пересланное — по своим сообщениям;
* ссылки в переписке и в «Избранном».

Лайков и реакций Telegram перечислить не даёт, истории просмотров в нём нет.

Главное преимущество перед сопоставлением по именам: живой доступ отдаёт
**внутренние числовые идентификаторы**. В реестре 393 записи опознаются
только по ним — имени канала там нет вовсе. Кроме того, совпадение по
идентификатору переживает переименование канала.

О безопасности
--------------
* Сессия держится только в памяти, файл сессии на диск не пишется.
* По завершении проверки программа выходит из аккаунта, чтобы в разделе
  «Устройства» не оставалось следа стороннего клиента.
* Пароль облачной защиты нигде не сохраняется.
* Чтение идёт размеренно и с серверными фильтрами, чтобы не выглядеть для
  Telegram нетипичной нагрузкой: за такое аккаунт получает ограничения.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from matching.observation import Observation, observe_url
from registry.normalize import find_urls

TELETHON_MISSING = (
    "Для проверки Telegram нужна библиотека Telethon.\n"
    "Установите её командой:  pip install telethon"
)

# Задержка между диалогами: ровная неспешная работа выглядит для Telegram
# обычным клиентом, а частые залпы запросов — поводом ограничить аккаунт.
PAUSE_BETWEEN_DIALOGS = 0.4

# Сколько сообщений смотреть в одном диалоге. Ограничение нужно, чтобы
# проверка не превращалась в многочасовое перелистывание всей переписки.
DEFAULT_MESSAGE_LIMIT = 3000

# Telegram сам просит подождать, когда темп чтения кажется ему высоким.
# Короткую паузу пережидаем, а требование ждать долго означает, что аккаунт
# уже на подозрении: тогда чтение прекращаем, чтобы не довести до блокировки.
MAX_FLOOD_WAIT = 60


def _flood_wait_seconds(error) -> int | None:
    """Сколько Telegram просит подождать, если это требование паузы.

    Определяется по имени класса, а не по импорту: модуль должен читаться
    и без установленной Telethon.
    """
    if "FloodWait" not in type(error).__name__:
        return None
    seconds = getattr(error, "seconds", None)
    return int(seconds) if isinstance(seconds, (int, float)) else 0


def require_telethon():
    """Подключить Telethon, объяснив по-человечески, если её нет."""
    try:
        import telethon  # noqa: F401
    except ImportError as exc:  # pragma: no cover - зависит от среды
        raise RuntimeError(TELETHON_MISSING) from exc
    return telethon


# --------------------------------------------------------------------------
# Превращение данных Telegram в наблюдения.
# Эта часть не обращается к сети и проверяется тестами целиком.
# --------------------------------------------------------------------------


def normalize_chat_id(value) -> str:
    """Привести идентификатор чата к виду, принятому в реестре.

    Telegram показывает идентификаторы каналов и в «сыром» виде
    (``1442177936``), и с приставкой ``-100``. В реестре они записаны
    без приставки, поэтому её снимаем.
    """
    digits = "".join(character for character in str(value) if character.isdigit())
    if digits.startswith("100") and len(digits) > 10:
        digits = digits[3:]
    return digits


def _usernames(entity) -> list[str]:
    """Все имена канала: у каналов их бывает несколько."""
    names: list[str] = []
    primary = getattr(entity, "username", None)
    if primary:
        names.append(primary)
    for extra in getattr(entity, "usernames", None) or []:
        name = getattr(extra, "username", None)
        if name and name not in names:
            names.append(name)
    return names


def is_subscription(entity) -> bool:
    """Подписка ли это.

    Личная переписка с человеком подпиской не является, поэтому в отчёт
    как «подписка» не попадает.
    """
    kind = type(entity).__name__
    if kind == "User":
        return False
    return kind in {"Channel", "Chat", "ChatForbidden", "ChannelForbidden"}


def observations_from_dialog(entity, title: str = "") -> list[Observation]:
    """Наблюдения из одной подписки.

    Отдаются и имя, и числовой идентификатор: имя канал может сменить,
    идентификатор — нет.
    """
    if not is_subscription(entity):
        return []

    where = title or getattr(entity, "title", "") or "без названия"
    source = f"Telegram: подписки — «{where}»"
    observations: list[Observation] = []

    for username in _usernames(entity):
        observations.append(
            Observation(
                platform="telegram",
                kind="handle",
                value=username.lower(),
                trace_type="subscription",
                source=source,
                raw=f"@{username}",
            )
        )

    chat_id = normalize_chat_id(getattr(entity, "id", ""))
    if chat_id:
        observations.append(
            Observation(
                platform="telegram",
                kind="numeric_id",
                value=chat_id,
                trace_type="subscription",
                source=source,
                raw=str(getattr(entity, "id", "")),
            )
        )
    return observations


def _forward_origin(message):
    """Откуда переслано сообщение, если это пересылка."""
    header = getattr(message, "fwd_from", None)
    if header is None:
        return None, None

    peer = getattr(header, "from_id", None)
    for attribute in ("channel_id", "user_id", "chat_id"):
        value = getattr(peer, attribute, None)
        if value:
            return normalize_chat_id(value), getattr(header, "from_name", None)
    return None, getattr(header, "from_name", None)


def _hidden_urls(message) -> list[str]:
    """Адреса, спрятанные за текстом ссылки.

    Обычный поиск по тексту их не найдёт: в сообщении виден только
    подписанный текст, а адрес лежит отдельно.
    """
    found: list[str] = []
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            found.append(url)
    return found


def observations_from_message(message, where: str) -> list[Observation]:
    """Наблюдения из одного сообщения: пересылка и ссылки.

    Проверяются сообщения обеих сторон. Материал, присланный собеседником,
    хранится в переписке пользователя — то есть является следом, — но в
    отчёте помечается как полученный, а не как отправленный им самим.
    """
    observations: list[Observation] = []
    date = getattr(message, "date", None)
    occurred_at = date.date().isoformat() if date is not None else None

    outgoing = bool(getattr(message, "out", False))
    author = "отправлено вами" if outgoing else "получено"

    origin_id, origin_name = _forward_origin(message)
    if origin_id:
        observations.append(
            Observation(
                platform="telegram",
                kind="numeric_id",
                value=origin_id,
                trace_type="repost" if outgoing else "repost_received",
                source=f"Telegram: пересланное в «{where}» ({author})",
                raw=origin_name or origin_id,
                occurred_at=occurred_at,
            )
        )

    text = getattr(message, "message", None) or ""
    trace_type = "link" if outgoing else "link_received"
    for url in list(find_urls(text)) + _hidden_urls(message):
        for observation in observe_url(
            url, trace_type, f"Telegram: ссылка в «{where}» ({author})", occurred_at
        ):
            observations.append(observation)

    return observations


# --------------------------------------------------------------------------
# Живая часть: разговор с Telegram
# --------------------------------------------------------------------------


@dataclass
class TelegramCheckSettings:
    """Настройки проверки."""

    api_id: int
    api_hash: str
    message_limit: int = DEFAULT_MESSAGE_LIMIT
    read_messages: bool = True
    # По умолчанию читаются сообщения обеих сторон: присланное собеседником
    # хранится в переписке пользователя и тоже является следом.
    own_messages_only: bool = False
    quick: bool = False
    logout_when_done: bool = True
    pause: float = PAUSE_BETWEEN_DIALOGS
    max_flood_wait: int = MAX_FLOOD_WAIT
    progress: object = None
    errors: list[str] = field(default_factory=list)


def _report(settings: TelegramCheckSettings, text: str) -> None:
    if callable(settings.progress):
        settings.progress(text)


async def _collect_subscriptions(client, settings, seen, dialogs):
    """Список подписок вместе с их идентификаторами."""
    async for dialog in client.iter_dialogs():
        dialogs.append(dialog)
        for observation in observations_from_dialog(dialog.entity, dialog.name):
            if observation.key not in seen:
                seen.add(observation.key)
                yield observation
    _report(settings, f"Подписок и диалогов: {len(dialogs)}")


async def _collect_messages(client, settings, seen, dialogs):
    """Пересылки и ссылки из сообщений, потоком и без сохранения."""
    # Каждый диалог читается ровно один раз — двойной проход удваивал бы
    # нагрузку на аккаунт без всякой пользы.
    #
    # По умолчанию читаются сообщения обеих сторон: материал, присланный
    # собеседником, хранится в переписке пользователя и потому является
    # следом. Серверного отбора по пересылкам не существует, поэтому здесь
    # нужен сплошной проход по последним сообщениям диалога.
    #
    # Быстрый режим берёт серверный отбор по ссылкам: читается во много раз
    # меньше, но пересылки без ссылки при этом не видны.
    sender = "me" if settings.own_messages_only else None
    message_filter = _url_filter() if settings.quick else None

    for number, dialog in enumerate(dialogs, 1):
        where = dialog.name or "без названия"
        _report(settings, f"[{number}/{len(dialogs)}] читается: {where}")

        try:
            async for message in client.iter_messages(
                dialog.entity,
                limit=settings.message_limit,
                filter=message_filter,
                from_user=sender,
            ):
                for observation in observations_from_message(message, where):
                    if observation.key not in seen:
                        seen.add(observation.key)
                        yield observation
        except Exception as exc:
            wait = _flood_wait_seconds(exc)
            if wait is None:
                # Доступ к диалогу может быть закрыт — обычное дело.
                settings.errors.append(f"{where}: {type(exc).__name__}")
            elif wait <= settings.max_flood_wait:
                _report(
                    settings,
                    f"Telegram просит подождать {wait} с — пауза, затем продолжим.",
                )
                await asyncio.sleep(wait)
            else:
                # Долгое ожидание означает, что мы уже выглядим подозрительно.
                # Продолжать — значит рисковать блокировкой аккаунта.
                settings.errors.append(
                    f"{where}: Telegram потребовал ждать {wait} с — чтение остановлено"
                )
                _report(
                    settings,
                    "Telegram ограничил темп чтения. Проверка сообщений "
                    "остановлена, чтобы не рисковать аккаунтом. "
                    "Подписки при этом уже проверены.",
                )
                return

        await asyncio.sleep(settings.pause)


def _url_filter():
    """Серверный отбор сообщений со ссылками.

    Он резко сокращает объём чтения: вместо всей переписки Telegram
    присылает только сообщения, где ссылка есть. Это и быстрее, и заметно
    безопаснее для аккаунта.
    """
    try:
        from telethon.tl.types import InputMessagesFilterUrl
    except ImportError:
        return None
    return InputMessagesFilterUrl


async def collect(client, settings: TelegramCheckSettings):
    """Все наблюдения из Telegram. Ничего не пишется на диск."""
    seen: set[str] = set()
    dialogs: list = []
    async for observation in _collect_subscriptions(client, settings, seen, dialogs):
        yield observation
    if settings.read_messages:
        async for observation in _collect_messages(client, settings, seen, dialogs):
            yield observation
