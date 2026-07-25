"""Приведение адресов из реестра к единому виду, пригодному для сравнения.

Задача модуля — превратить пёстрый текст реестра
(``https://vk.соm/hodnaby,``, ``www.Instagram.com/Foo/``, ``t.me/s/bar``)
в устойчивый ключ вида ``vk:hodnaby``, ``instagram:foo``, ``telegram:bar``,
по которому потом сравниваются следы из выгрузок пользователя.

Основные шаги: чистка «мусора» вокруг адреса -> исправление омоглифов ->
исправление известных опечаток в доменах -> определение платформы ->
извлечение канонического идентификатора.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from .confusables import (
    cyrillic_variant,
    domain_to_latin,
    fix_segment,
    has_non_latin,
    is_mixed_script,
    latin_variant,
    to_latin,
)

# --------------------------------------------------------------------------
# Чистка сырого текста адреса
# --------------------------------------------------------------------------

# Реестр набирают в Word: адреса склеиваются с пунктуацией предложения.
# 3014 адресов в исходном файле заканчиваются точкой, 1606 — точкой с запятой.
_TRAILING_JUNK = ".,;:!?«»\"'()[]<>–—“”„"

# Куски, которые Word/HTML оставляет внутри адреса.
_INLINE_JUNK = re.compile(r"(&nbsp;| |\s)+")


def clean_raw_url(raw: str) -> str:
    """Убрать HTML-сущности, невидимые пробелы и пунктуацию вокруг адреса."""
    text = html.unescape(raw.strip())
    # Мягкие переносы и нулевой пробел, которые Word вставляет в длинные ссылки.
    text = text.replace("­", "").replace("​", "").replace("﻿", "")
    # Адрес обрывается на первом настоящем пробеле: всё после — уже текст.
    text = _INLINE_JUNK.split(text)[0]
    # В реестре «;» и «,» разделяют «зеркала», а пробел после них ставят не
    # всегда: без этого к имени аккаунта прилипает следующее слово
    # («cbswarsaw;сообщество»).
    for separator in (";", ","):
        text = text.split(separator)[0]
    return text.strip(_TRAILING_JUNK)


# --------------------------------------------------------------------------
# Домены
# --------------------------------------------------------------------------

# Опечатки составителей реестра, найденные в исходном файле, и синонимы
# доменов. Список получен сверкой всех доменов реестра с названиями площадок.
DOMAIN_TYPOS: dict[str, str] = {
    # синонимы и сокращённые формы
    "threads.net": "threads.com",
    "twitter.com": "x.com",
    "x.twitter.com": "x.com",
    "vk.ru": "vk.com",
    "telegram.me": "t.me",
    "telegram.dog": "t.me",
    "fb.com": "facebook.com",
    "fb.me": "facebook.com",
    "instagr.am": "instagram.com",
    "vm.tiktok.com": "tiktok.com",
    "vt.tiktok.com": "tiktok.com",
    "odnoklassniki.ru": "ok.ru",
    # опечатки составителей
    "treads.com": "threads.com",
    "theads.com": "threads.com",
    "threads.eom": "threads.com",
    "tiktok.eom": "tiktok.com",
    "youtube.eom": "youtube.com",
    "yotube.com": "youtube.com",
    "youtub.com": "youtube.com",
    "instgram.com": "instagram.com",
    "intagram.com": "instagram.com",
    "instagramm.com": "instagram.com",
    "facebok.com": "facebook.com",
    "faceboook.com": "facebook.com",
    "twiter.com": "x.com",
}

# Служебные поддомены, не влияющие на личность ресурса.
# Снимаются повторно: в реестре встречается ``m.www.youtube.com``.
_STRIP_SUBDOMAINS = ("www.", "m.", "web.", "l.", "mobile.", "ru-ru.", "en.", "ru.")

# Площадки-посредники: сами по себе они ничего не значат. Их присутствие в
# истории браузера есть почти у каждого, поэтому отдельным признаком для
# проверки они быть не могут — иначе программа завалит человека ложными
# срабатываниями. Конкретная ссылка внутри такого домена по-прежнему учитывается.
GENERIC_HOSTS = frozenset(
    """
    apps.apple.com play.google.com itunes.apple.com
    archive.is archive.org web.archive.org
    cdn.ampproject.org ampproject.org
    docs.google.com drive.google.com sites.google.com
    bit.ly tinyurl.com goo.gl cutt.ly is.gd tinyurl.ru clck.ru
    anchor.fm podcasts.apple.com open.spotify.com
    wa.me chat.whatsapp.com
    """.split()
)

# Сокращатели ссылок: домен не признак, а вот полный короткий адрес — признак.
SHORTENERS = frozenset(
    "bit.ly tinyurl.com goo.gl cutt.ly is.gd tinyurl.ru clck.ru".split()
)

PLATFORM_BY_DOMAIN: dict[str, str] = {
    "t.me": "telegram",
    "instagram.com": "instagram",
    "vk.com": "vk",
    "facebook.com": "facebook",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "ok.ru": "ok",
    "odnoklassniki.ru": "ok",
    "x.com": "x",
    "threads.com": "threads",
    "linkedin.com": "linkedin",
    "soundcloud.com": "soundcloud",
    "linktr.ee": "linktree",
    "taplink.cc": "taplink",
    "patreon.com": "patreon",
    "invite.viber.com": "viber",
}


def normalize_host(host: str) -> tuple[str, list[str]]:
    """Привести доменное имя к каноническому виду.

    Возвращает ``(домен, список_заметок_об_исправлениях)``.

    Доменные имена по стандарту латинские, поэтому кириллицу внутри домена
    исправляем безусловно — это всегда опечатка (``vk.соm``, ``оk.ru``).
    """
    notes: list[str] = []
    host = host.strip().strip(".").lower()

    if has_non_latin(host):
        # В домене кириллица недопустима, поэтому применяется расширенная
        # таблица: она ловит и «различимые» буквы вроде 'к' в ``vк.соm``.
        fixed = domain_to_latin(host)
        if fixed != host:
            notes.append(f"домен: исправлены омоглифы {host!r} -> {fixed!r}")
            host = fixed

    # Префиксы и опечатки снимаются по очереди до тех пор, пока домен
    # меняется: в реестре встречаются наслоения вроде ``www.m.facebook.com``.
    for _ in range(6):
        before = host
        if host in DOMAIN_TYPOS:
            fixed = DOMAIN_TYPOS[host]
            notes.append(f"домен: {host!r} -> {fixed!r}")
            host = fixed
        for prefix in _STRIP_SUBDOMAINS:
            if host.startswith(prefix) and host.count(".") > 1:
                host = host[len(prefix) :]
                break
        if host == before:
            break

    return host, notes


def is_checkable_domain(host: str) -> bool:
    """Годится ли домен как самостоятельный признак для проверки.

    Отсеиваются посредники (``apps.apple.com``, сокращатели ссылок) и
    заведомо битые значения без домена верхнего уровня.
    """
    if host in GENERIC_HOSTS:
        return False
    if "." not in host:
        return False
    labels = host.split(".")
    if len(labels[0]) < 2:
        return False
    # Домен второго уровня у посредника (``foo.cdn.ampproject.org``)
    # тоже проверять бессмысленно.
    return not any(
        host.endswith("." + generic) for generic in GENERIC_HOSTS if "." in generic
    )


def normalize_path_segment(segment: str) -> tuple[str, list[str]]:
    """Исправить омоглифы в одном сегменте пути, если это опечатка."""
    notes: list[str] = []
    decoded = unquote(segment)
    # После раскрытия «%20» в адресе может оказаться пробел, а за ним —
    # прилипший текст («strokabelarus%20и%203»). Имя аккаунта пробелов
    # не содержит ни на одной площадке, поэтому обрываем адрес здесь.
    decoded = re.split(r"[\s\xa0]", decoded)[0]
    fixed, changed = fix_segment(decoded)
    if changed:
        notes.append(f"адрес: исправлены омоглифы {decoded!r} -> {fixed!r}")
    return fixed, notes


# --------------------------------------------------------------------------
# Идентификатор
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Identifier:
    """Один опознавательный признак ресурса из реестра."""

    platform: str
    kind: str  # handle | numeric_id | invite | stickerset | video | domain | url
    value: str  # нормализованное значение, по нему идёт сравнение
    raw: str  # как было написано в реестре
    notes: tuple[str, ...] = field(default=())

    @property
    def key(self) -> str:
        """Ключ для сравнения со следами пользователя."""
        return f"{self.platform}:{self.kind}:{self.value}"


def _handle(platform: str, value: str, raw: str, notes: list[str]) -> Identifier:
    return Identifier(platform, "handle", value.lower(), raw, tuple(notes))


# Буквы, которыми в цифровом идентификаторе оказывается опечатка набора.
_DIGIT_LOOKALIKES = {"б": "6", "о": "0", "О": "0", "з": "3", "З": "3", "І": "1", "l": "1"}


def _numeric_or_handle(
    platform: str, value: str, raw: str, notes: list[str]
) -> list[Identifier]:
    """Числовой идентификатор, устойчивый к опечаткам в цифрах.

    В реестре встречается ``ok.ru/group/53538910б99605`` — кириллическая
    буква посреди цифр. Однозначно восстановить исходное число нельзя,
    поэтому в индекс идут оба правдоподобных прочтения: буква как похожая
    цифра и буква как лишний символ.
    """
    if value.isdigit():
        return [Identifier(platform, "numeric_id", value, raw, tuple(notes))]

    digits = sum(character.isdigit() for character in value)
    if digits < 4 or digits / len(value) <= 0.7:
        return [_handle(platform, value, raw, notes)]

    note = notes + [f"в цифровом идентификаторе {value!r} есть буквы"]
    readings: list[str] = []
    for candidate in (
        "".join(_DIGIT_LOOKALIKES.get(ch, ch) for ch in value),
        "".join(ch for ch in value if ch.isdigit()),
    ):
        if candidate.isdigit() and candidate not in readings:
            readings.append(candidate)

    if not readings:
        return [_handle(platform, value, raw, notes)]
    return [
        Identifier(platform, "numeric_id", reading, raw, tuple(note))
        for reading in readings
    ]


# Разделы платформ, которые не являются именем аккаунта.
_RESERVED = {
    "telegram": {"s", "c", "joinchat", "addstickers", "share", "proxy", "socks"},
    "vk": {"wall", "video", "videos", "album", "albums", "photo", "topic", "away"},
    "facebook": {"groups", "profile.php", "people", "share", "pages", "watch", "pg"},
    "youtube": {"channel", "c", "user", "watch", "shorts", "playlist", "results"},
    "ok": {"group", "profile", "video", "videos", "topic", "dk"},
    "instagram": {"p", "reel", "reels", "tv", "stories", "explore"},
    "tiktok": {"video", "tag", "music", "discover"},
    "x": {"i", "status", "hashtag", "search", "intent"},
}


def _split_path(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def parse_url(raw: str) -> list[Identifier]:
    """Разобрать один адрес в набор идентификаторов.

    Обычно возвращает один идентификатор. Несколько — когда написание
    неоднозначно и безопаснее проиндексировать оба варианта, чтобы
    гарантированно не пропустить совпадение.
    """
    cleaned = clean_raw_url(raw)
    if not cleaned:
        return []

    if "//" not in cleaned:
        cleaned = "https://" + cleaned

    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return []

    netloc = parts.netloc
    if "@" in netloc:
        # Запись вида ``https://kolektiva.social@abcbelarus`` — это адрес в
        # Mastodon, записанный слитно. Настоящий домен стоит до «собаки»;
        # без этой поправки в индекс попадает несуществующий домен.
        netloc = netloc.split("@", 1)[0]

    hostname = netloc.split(":")[0]
    if not hostname:
        return []

    host, notes = normalize_host(hostname)
    platform = PLATFORM_BY_DOMAIN.get(host)

    segments: list[str] = []
    for seg in _split_path(parts.path):
        fixed, seg_notes = normalize_path_segment(seg)
        notes.extend(seg_notes)
        segments.append(fixed)

    if platform is None:
        if host in SHORTENERS and segments:
            # Сокращённая ссылка. Сам домен проверять бессмысленно, но
            # конкретный адрес — это ресурс реестра, и в истории браузера
            # он может встретиться ровно в таком виде.
            return [
                Identifier(
                    "web",
                    "shortlink",
                    f"{host}/{segments[0]}",
                    raw,
                    tuple(notes + ["короткая ссылка: настоящий адрес неизвестен"]),
                )
            ]
        # Не соцсеть — значит это сайт из перечня доменов.
        if not is_checkable_domain(host):
            return []
        return [Identifier("web", "domain", host, raw, tuple(notes))]

    handler = _PLATFORM_HANDLERS.get(platform)
    identifiers = handler(host, segments, parts.query, raw, notes) if handler else []

    # Смешанные написания однозначно разрешить нельзя: «НАШАMOBA» — это
    # кириллическое «НАШАМОВА», а «раra2022» — латинское «para2022».
    # Поэтому оба варианта попадают в индекс: лишний ключ безвреден,
    # а пропущенное совпадение — нет.
    extra: list[Identifier] = []
    for ident in identifiers:
        if ident.kind == "domain":
            continue  # домен уже приведён к латинице однозначно
        if not is_mixed_script(ident.value):
            # Чисто латинское или чисто кириллическое написание однозначно —
            # придумывать ему двойника нельзя, иначе индекс засоряется.
            continue
        for alt, note in (
            (latin_variant(ident.value), "добавлено латинское написание"),
            (cyrillic_variant(ident.value), "добавлено кириллическое написание"),
        ):
            if alt and alt != ident.value:
                extra.append(
                    Identifier(
                        ident.platform, ident.kind, alt, raw, ident.notes + (note,)
                    )
                )
    return identifiers + extra


# --- разборщики по платформам ---------------------------------------------


def _telegram(host, seg, query, raw, notes) -> list[Identifier]:
    if not seg:
        return []
    head = seg[0]
    low = to_latin(head).lower()

    if low == "s" and len(seg) > 1:  # t.me/s/<handle> — публичный просмотр
        return [_handle("telegram", seg[1], raw, notes)]
    if low == "c" and len(seg) > 1:  # t.me/c/<внутренний id>
        digits = re.sub(r"\D", "", seg[1])
        if digits:
            return [Identifier("telegram", "numeric_id", digits, raw, tuple(notes))]
        return []
    if low == "joinchat" and len(seg) > 1:
        return [Identifier("telegram", "invite", seg[1], raw, tuple(notes))]
    if head.startswith("+"):
        return [Identifier("telegram", "invite", head[1:], raw, tuple(notes))]
    if low == "addstickers" and len(seg) > 1:
        return [Identifier("telegram", "stickerset", seg[1].lower(), raw, tuple(notes))]
    if low in _RESERVED["telegram"]:
        return []
    return [_handle("telegram", head, raw, notes)]


def _vk(host, seg, query, raw, notes) -> list[Identifier]:
    if not seg:
        return []
    head = seg[0]
    low = head.lower()

    # vk.com/public123, club123, id123, event123 — числовой владелец
    m = re.fullmatch(r"(public|club|id|event|group)(\d+)", low)
    if m:
        return [Identifier("vk", "numeric_id", m.group(2), raw, tuple(notes))]
    # vk.com/wall-123_456, video-123_456 — владелец в первом числе
    m = re.fullmatch(r"(wall|video|photo|album|topic)(-?\d+)_\d+", low)
    if m:
        return [
            Identifier("vk", "numeric_id", m.group(2).lstrip("-"), raw, tuple(notes))
        ]
    if low in _RESERVED["vk"]:
        return []
    return [_handle("vk", head, raw, notes)]


def _facebook(host, seg, query, raw, notes) -> list[Identifier]:
    if not seg:
        return []
    head = seg[0]
    low = head.lower()

    if low == "profile.php":
        ids = parse_qs(query).get("id", [])
        if ids:
            return [
                Identifier("facebook", "numeric_id", ids[0].strip(), raw, tuple(notes))
            ]
        return []
    if low == "groups" and len(seg) > 1:
        return [Identifier("facebook", "group", seg[1].lower(), raw, tuple(notes))]
    if low == "people" and len(seg) > 2:
        return [Identifier("facebook", "numeric_id", seg[2], raw, tuple(notes))]
    if low == "pg" and len(seg) > 1:
        # Устаревшая форма адреса страницы: facebook.com/pg/<имя>
        return [_handle("facebook", seg[1], raw, notes)]
    if low == "watch":
        values = parse_qs(query).get("v", [])
        if values:
            return [Identifier("facebook", "video", values[0], raw, tuple(notes))]
        return []
    if low.isdigit():
        return [Identifier("facebook", "numeric_id", low, raw, tuple(notes))]
    if low in _RESERVED["facebook"]:
        # /share/... — временная ссылка, личность из неё не извлекается.
        return []
    return [_handle("facebook", head, raw, notes)]


def _instagram(host, seg, query, raw, notes) -> list[Identifier]:
    if not seg:
        return []
    head = seg[0].lstrip("@")
    low = head.lower()
    # Реестр нередко называет конкретную публикацию, а не аккаунт целиком.
    # Такие ссылки нужны: именно они встречаются в лайках и сохранённом.
    if low in {"p", "reel", "reels", "tv"} and len(seg) > 1:
        return [Identifier("instagram", "post", seg[1], raw, tuple(notes))]
    if low in _RESERVED["instagram"]:
        return []
    return [_handle("instagram", head, raw, notes)]


def _tiktok(host, seg, query, raw, notes) -> list[Identifier]:
    found: list[Identifier] = []
    for index, part in enumerate(seg):
        if part.startswith("@") and len(part) > 1:
            found.append(_handle("tiktok", part[1:], raw, notes))
        elif part.lower() == "video" and index + 1 < len(seg):
            found.append(
                Identifier("tiktok", "video", seg[index + 1], raw, tuple(notes))
            )
    if found:
        return found
    if seg and seg[0].lower() not in _RESERVED["tiktok"]:
        return [_handle("tiktok", seg[0], raw, notes)]
    return []


def _youtube(host, seg, query, raw, notes) -> list[Identifier]:
    if host == "youtu.be":
        # Короткая ссылка youtu.be/<id> — всегда конкретный ролик.
        if seg:
            return [Identifier("youtube", "video", seg[0], raw, tuple(notes))]
        return []
    if not seg:
        return []
    head = seg[0]
    low = head.lower()

    if low == "channel" and len(seg) > 1:
        return [Identifier("youtube", "channel_id", seg[1], raw, tuple(notes))]
    if low in {"c", "user"} and len(seg) > 1:
        return [_handle("youtube", seg[1], raw, notes)]
    if head.startswith("@") and len(head) > 1:
        return [_handle("youtube", head[1:], raw, notes)]
    if low == "watch":
        # Конкретный ролик — проверяется по истории просмотров.
        values = parse_qs(query).get("v", [])
        if values:
            fixed, extra = normalize_path_segment(values[0])
            return [Identifier("youtube", "video", fixed, raw, tuple(notes + extra))]
        return []
    if low == "shorts" and len(seg) > 1:
        return [Identifier("youtube", "video", seg[1], raw, tuple(notes))]
    if low in {"playlist", "results"}:
        return []
    return [_handle("youtube", head, raw, notes)]


def _ok(host, seg, query, raw, notes) -> list[Identifier]:
    if not seg:
        return []
    head = seg[0]
    low = head.lower()
    if low in {"group", "profile"} and len(seg) > 1:
        return _numeric_or_handle("ok", seg[1], raw, notes)
    # Косую черту в реестре иногда пропускают: ``ok.ru/group63138604843193``.
    glued = re.fullmatch(r"(group|profile)(\d.*)", low)
    if glued:
        return _numeric_or_handle("ok", glued.group(2), raw, notes)
    if low == "dk":
        # Старая форма: ok.ru/dk?st.cmd=altGroupMain&st.groupId=648198885
        params = parse_qs(query)
        for name in ("st.groupId", "st.groupid", "st.friendId"):
            values = params.get(name)
            if values:
                return [Identifier("ok", "numeric_id", values[0], raw, tuple(notes))]
        return []
    if low in _RESERVED["ok"]:
        return []
    return [_handle("ok", head, raw, notes)]


def _x(host, seg, query, raw, notes) -> list[Identifier]:
    if not seg:
        return []
    head = seg[0].lstrip("@")
    if head.lower() in _RESERVED["x"]:
        return []
    return [_handle("x", head, raw, notes)]


def _simple(platform: str):
    def handler(host, seg, query, raw, notes) -> list[Identifier]:
        if not seg:
            return []
        return [_handle(platform, seg[0].lstrip("@"), raw, notes)]

    return handler


_PLATFORM_HANDLERS = {
    "telegram": _telegram,
    "vk": _vk,
    "facebook": _facebook,
    "instagram": _instagram,
    "tiktok": _tiktok,
    "youtube": _youtube,
    "ok": _ok,
    "x": _x,
    "threads": _simple("threads"),
    "linkedin": _simple("linkedin"),
    "soundcloud": _simple("soundcloud"),
    "linktree": _simple("linktree"),
    "taplink": _simple("taplink"),
    "patreon": _simple("patreon"),
    "viber": _simple("viber"),
}


# --------------------------------------------------------------------------
# Идентификаторы, записанные не ссылкой
# --------------------------------------------------------------------------

# «идентификатор (ID), содержащий последовательность цифр 1442177936»
# В реестре встречается и без пробела перед числом.
NUMERIC_ID_RE = re.compile(r"последовательност[ьи]\s+цифр\s*:?\s*(\d[\d\s]*)", re.I)

# «@handle», написанный без ссылки.
BARE_HANDLE_RE = re.compile(r"(?<![\w/@.])@([A-Za-z][\w.]{3,31})\b")

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'«»]+", re.I)

# Домены верхнего уровня, встречающиеся в реестре или правдоподобные для него.
# Список нужен именно закрытый: без него в «домены» попадают куски текста
# и имена файлов — ``материал.wmv``, ``index.php``, ``ilovepublic``.
KNOWN_TLDS = frozenset(
    """
    by ru ua pl lt lv ee de fr uk eu us ca cz sk md ge kz am
    com net org info biz name pro site online top live life news media
    io me cc tv co app link click space website store blog press
    team world today info page one xyz art fm ga tk ml cf
    """.split()
)

# Домен, записанный без «http://»: ``misanthropic.info``, ``holokosta.blogspot.com.by``
BARE_DOMAIN_RE = re.compile(
    r"(?<![\w/@.-])((?:[a-z0-9][a-z0-9-]{0,62}\.)+[a-z]{2,12})(?![\w@.-])", re.I
)


def find_bare_domains(text: str) -> list[str]:
    """Найти домены, записанные без протокола.

    Такие записи в реестре встречаются регулярно, а для проверки истории
    браузера они не менее важны, чем полноценные ссылки.
    """
    # Полные ссылки уже разобраны отдельно — убираем, чтобы не дублировать.
    without_urls = URL_RE.sub(" ", text)
    found: list[str] = []
    for candidate in BARE_DOMAIN_RE.findall(without_urls):
        domain = candidate.lower().strip(".")
        if domain.rsplit(".", 1)[-1] not in KNOWN_TLDS:
            continue
        # Отсекаем «слова с точкой» вроде ``2011г.``: нужен хотя бы один
        # осмысленный по длине уровень перед доменом верхнего уровня.
        labels = domain.split(".")
        if len(labels) < 2 or len(labels[0]) < 2:
            continue
        found.append(domain)
    return found


def find_numeric_ids(text: str) -> list[str]:
    """Найти числовые идентификаторы, записанные словами."""
    found: list[str] = []
    for match in NUMERIC_ID_RE.finditer(text):
        digits = re.sub(r"\s", "", match.group(1))
        if digits:
            found.append(digits)
    return found


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text)


def find_bare_handles(text: str) -> list[str]:
    return BARE_HANDLE_RE.findall(text)


def normalize_text(text: str) -> str:
    """Свернуть текст для нестрогого сравнения названий.

    Используется только для подсказок «проверьте вручную» (книги, музыка),
    не для точных совпадений.
    """
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()
