"""Поддельный Telegram для проверок.

Повторяет то, как выглядят данные настоящего Telethon: типы объектов
различаются по имени класса, идентификаторы бывают с приставкой и без,
адреса бывают спрятаны за текстом ссылки.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


# Названия классов важны: программа отличает подписку от личной переписки
# именно по типу объекта, как это делает Telethon.
class Channel:
    def __init__(self, id, title, username=None, usernames=(), broadcast=True):
        self.id = id
        self.title = title
        self.username = username
        self.usernames = list(usernames)
        self.broadcast = broadcast


class Chat:
    def __init__(self, id, title):
        self.id = id
        self.title = title
        self.username = None


class User:
    def __init__(self, id, username=None, first_name=""):
        self.id = id
        self.username = username
        self.first_name = first_name


@dataclass
class Username:
    """Дополнительное имя канала (у каналов их бывает несколько)."""

    username: str


@dataclass
class PeerChannel:
    channel_id: int


@dataclass
class PeerUser:
    user_id: int


@dataclass
class ForwardHeader:
    from_id: object = None
    from_name: str | None = None


@dataclass
class UrlEntity:
    """Ссылка, спрятанная за подписанным текстом."""

    url: str


@dataclass
class Message:
    message: str = ""
    entities: list = field(default_factory=list)
    fwd_from: ForwardHeader | None = None
    date: datetime | None = None
    out: bool = True


@dataclass
class Dialog:
    entity: object
    name: str


class FakeClient:
    """Клиент Telegram с заранее заданными данными."""

    def __init__(self, dialogs, messages=None, unreadable=()):
        self._dialogs = dialogs
        self._messages = messages or {}
        self._unreadable = set(unreadable)
        self.logged_out = False
        self.disconnected = False
        self.requests: list[tuple] = []

    async def iter_dialogs(self):
        for dialog in self._dialogs:
            yield dialog

    async def iter_messages(self, entity, limit=None, filter=None, from_user=None):
        # Ключом служит идентификатор: у личных диалогов названия нет.
        key = entity.id
        self.requests.append((key, limit, filter is not None, from_user))
        if key in self._unreadable:
            raise PermissionError("нет доступа к диалогу")
        # Серверный отбор по ссылкам возвращает только сообщения со ссылками,
        # как это делает настоящий Telegram.
        for message in self._messages.get(key, []):
            if filter is not None and not (message.message or message.entities):
                continue
            yield message

    async def log_out(self):
        self.logged_out = True
        return True

    async def disconnect(self):
        self.disconnected = True
