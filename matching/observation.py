"""«Наблюдение» — один след, найденный в данных пользователя.

Наблюдения приходят из разных источников: архивов выгрузок, живых
коннекторов, истории браузера. Все они приводятся к одному виду, чтобы
движок сопоставления не знал, откуда пришли данные.

Важное правило: адреса пользователя проходят через **тот же** нормализатор,
что и адреса реестра (:mod:`registry.normalize`). Иначе одна и та же ссылка
превратилась бы в разные ключи и совпадение потерялось бы.
"""

from __future__ import annotations

from dataclasses import dataclass

from registry.normalize import Identifier, find_bare_domains, normalize_host, parse_url

# Виды следов. Формулировки нужны в отчёте, поэтому хранятся рядом.
TRACE_TYPES: dict[str, str] = {
    "subscription": "подписка",
    "like": "лайк",
    "repost": "репост или пересланное",
    "view": "история просмотров",
    "link": "ссылка в переписке",
    "saved": "сохранённое или закладки",
    "search": "поисковый запрос",
    "comment": "комментарий",
    "visit": "посещение сайта",
}


@dataclass(frozen=True)
class Observation:
    """Один след пользователя, приведённый к сравнимому виду."""

    platform: str
    kind: str
    value: str
    trace_type: str
    source: str  # где именно найдено: файл выгрузки, диалог, раздел
    raw: str = ""
    occurred_at: str | None = None

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.kind}:{self.value}"

    @property
    def trace_label(self) -> str:
        return TRACE_TYPES.get(self.trace_type, self.trace_type)


def _from_identifier(
    identifier: Identifier,
    trace_type: str,
    source: str,
    occurred_at: str | None,
) -> Observation:
    return Observation(
        platform=identifier.platform,
        kind=identifier.kind,
        value=identifier.value,
        trace_type=trace_type,
        source=source,
        raw=identifier.raw or identifier.value,
        occurred_at=occurred_at,
    )


def observe_url(
    url: str,
    trace_type: str,
    source: str,
    occurred_at: str | None = None,
) -> list[Observation]:
    """Превратить ссылку из данных пользователя в наблюдения."""
    return [
        _from_identifier(identifier, trace_type, source, occurred_at)
        for identifier in parse_url(url)
    ]


def observe_handle(
    platform: str,
    handle: str,
    trace_type: str,
    source: str,
    occurred_at: str | None = None,
) -> Observation:
    """Наблюдение по имени аккаунта, известному без ссылки.

    Так приходят подписки из большинства выгрузок: там указано имя канала,
    а не полный адрес.
    """
    return Observation(
        platform=platform,
        kind="handle",
        value=handle.strip().lstrip("@").lower(),
        trace_type=trace_type,
        source=source,
        raw=handle,
        occurred_at=occurred_at,
    )


def observe_numeric_id(
    platform: str,
    identifier: str,
    trace_type: str,
    source: str,
    occurred_at: str | None = None,
) -> Observation:
    """Наблюдение по внутреннему числовому идентификатору.

    Доступно там, где выгрузка или живой коннектор их показывает —
    прежде всего Telegram и ВКонтакте. Только такие наблюдения способны
    совпасть с записями реестра, где указан один числовой ID без ссылки.
    """
    return Observation(
        platform=platform,
        kind="numeric_id",
        value="".join(ch for ch in str(identifier) if ch.isdigit()),
        trace_type=trace_type,
        source=source,
        raw=str(identifier),
        occurred_at=occurred_at,
    )


def observe_visited_domain(
    url_or_domain: str,
    source: str,
    occurred_at: str | None = None,
) -> list[Observation]:
    """Наблюдение из истории браузера.

    История браузера ценна тем, что не требует ждать выгрузку: она читается
    с диска сразу. Сверяется с перечнем сайтов из реестра.
    """
    observations = observe_url(url_or_domain, "visit", source, occurred_at)
    if observations:
        return observations
    for domain in find_bare_domains(url_or_domain):
        host, _ = normalize_host(domain)
        return [
            Observation("web", "domain", host, "visit", source, url_or_domain, occurred_at)
        ]
    return []
