"""Клиент OPDS 1.2: чтение чужих каталогов и скачивание FB2.

Поддерживается Atom/OPDS 1.2. OPDS 2 (JSON) намеренно не поддерживается:
для домашней библиотеки достаточно OPDS 1.2, который отдают Calibre,
KOReader-совместимые каталоги и большинство серверов.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

import xml.etree.ElementTree as ET

from .html_sanitizer import html_to_text, sanitize_html

ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/terms/"
OPDS_NS = "http://opds-spec.org/2010/catalog"

NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
FB2_TYPES = {
    "application/fb2",
    "application/fb2+xml",
    "application/fb2+zip",
    "application/x-fictionbook+xml",
    "application/x-fictionbook",
}
ACQ_REL = "http://opds-spec.org/acquisition"

_ILLEGAL_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACE_RE = re.compile(r"\s+")


class OpdsError(RuntimeError):
    """Ошибка чтения или скачивания OPDS."""


@dataclass(slots=True)
class FeedLink:
    rel: str
    href: str
    type: str = ""
    title: str = ""
    length: int | None = None


@dataclass(slots=True)
class FeedEntry:
    title: str
    id: str = ""
    updated: str = ""
    authors: list[str] = field(default_factory=list)
    summary: str = ""
    summary_html: str = ""
    categories: list[str] = field(default_factory=list)
    language: str = ""
    issued: str = ""
    publisher: str = ""
    identifier: str = ""
    series: str = ""
    series_number: str = ""
    links: list[FeedLink] = field(default_factory=list)

    def navigation_link(self) -> FeedLink | None:
        for link in self.links:
            if is_navigation_link(link):
                return link
        return None

    def acquisition_links(self) -> list[FeedLink]:
        return [link for link in self.links if is_acquisition_link(link)]


@dataclass(slots=True)
class Feed:
    title: str
    url: str = ""
    id: str = ""
    updated: str = ""
    links: list[FeedLink] = field(default_factory=list)
    entries: list[FeedEntry] = field(default_factory=list)

    def link_by_rel(self, rel: str) -> FeedLink | None:
        for link in self.links:
            if link.rel == rel:
                return link
        return None


@dataclass(slots=True)
class DownloadResult:
    path: Path
    size: int
    content_type: str = ""
    sha256: str = ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean(value: str | None) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


def _text(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    return _clean(" ".join(elem.itertext()))


def _first_text(entry: ET.Element, *names: str) -> str:
    for name in names:
        for child in entry:
            if _local(child.tag) == name:
                value = _text(child)
                if value:
                    return value
    return ""


def _parse_link(elem: ET.Element, base_url: str) -> FeedLink | None:
    href = _clean(elem.get("href"))
    if not href:
        return None
    href = urljoin(base_url, href)
    rel = _clean(elem.get("rel"))
    type_ = _clean(elem.get("type")).split(";", 1)[0].strip().lower()
    length: int | None = None
    raw_length = _clean(elem.get("length"))
    if raw_length.isdigit():
        length = int(raw_length)
    return FeedLink(
        rel=rel,
        href=href,
        type=type_,
        title=_clean(elem.get("title")),
        length=length,
    )


def _inner_html(elem: ET.Element) -> str:
    if not len(elem):
        return elem.text or ""
    parts: list[str] = [elem.text or ""]
    for child in elem:
        parts.append(ET.tostring(child, encoding="unicode"))
        parts.append(child.tail or "")
    return "".join(parts)


def _entry_description(elem: ET.Element, base_url: str) -> tuple[str, str]:
    """Вернуть (плоский текст, безопасный HTML) описания."""
    for child in elem:
        if _local(child.tag) not in {"content", "summary", "description"}:
            continue
        content_type = _clean(child.get("type")).lower().split(";", 1)[0]
        fragment = _inner_html(child)
        if content_type in {"html", "text/html", "xhtml", "application/xhtml+xml"}:
            safe = sanitize_html(fragment, base_url)
            return html_to_text(safe) or _text(child), safe

        # Некоторые каталоги кладут HTML в content без type="html".
        if re.search(r"</?(?:p|br|b|strong|i|em|ul|ol|li|blockquote)\b", fragment, re.I):
            safe = sanitize_html(fragment, base_url)
            if safe:
                return html_to_text(safe), safe
        return _text(child), ""
    return "", ""


def _parse_entry(elem: ET.Element, base_url: str) -> FeedEntry:
    authors: list[str] = []
    for author in elem:
        if _local(author.tag) != "author":
            continue
        name = _first_text(author, "name")
        if not name:
            name = _clean(
                " ".join(
                    filter(
                        None,
                        [
                            _first_text(author, "first-name"),
                            _first_text(author, "middle-name"),
                            _first_text(author, "last-name"),
                        ],
                    )
                )
            )
        if name and name not in authors:
            authors.append(name)

    categories: list[str] = []
    for category in elem:
        if _local(category.tag) != "category":
            continue
        label = _clean(category.get("label")) or _clean(category.get("term"))
        if label and label not in categories:
            categories.append(label)

    summary, summary_html = _entry_description(elem, base_url)
    series = ""
    series_number = ""
    for child in elem:
        if _local(child.tag) == "series":
            series = _clean(child.get("name"))
            series_number = _clean(child.get("number"))
            break
    if not series:
        series = _first_text(elem, "isPartOf")

    links = [
        link
        for link in (
            _parse_link(child, base_url)
            for child in elem
            if _local(child.tag) == "link"
        )
        if link is not None
    ]

    return FeedEntry(
        title=_first_text(elem, "title") or "Без названия",
        id=_first_text(elem, "id"),
        updated=_first_text(elem, "updated"),
        authors=authors,
        summary=summary,
        summary_html=summary_html,
        categories=categories,
        language=_first_text(elem, "language"),
        issued=_first_text(elem, "issued"),
        publisher=_first_text(elem, "publisher"),
        identifier=_first_text(elem, "identifier"),
        series=series,
        series_number=series_number,
        links=links,
    )


def parse_feed(data: bytes, base_url: str) -> Feed:
    """Разобрать Atom/OPDS-фид. ``base_url`` нужен для относительных ссылок."""
    _validate_url(base_url)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise OpdsError(f"источник вернул некорректный XML: {exc}") from exc

    if _local(root.tag) != "feed":
        raise OpdsError("источник вернул не Atom/OPDS-фид")

    links = [
        link
        for link in (
            _parse_link(child, base_url)
            for child in root
            if _local(child.tag) == "link"
        )
        if link is not None
    ]
    entries = [
        _parse_entry(child, base_url)
        for child in root
        if _local(child.tag) == "entry"
    ]
    return Feed(
        title=_first_text(root, "title") or base_url,
        url=base_url,
        id=_first_text(root, "id"),
        updated=_first_text(root, "updated"),
        links=links,
        entries=entries,
    )


def is_navigation_link(link: FeedLink) -> bool:
    if link.rel == "subsection" or "kind=navigation" in link.type:
        return True
    if link.rel.startswith("http://opds-spec.org/sort/"):
        return True
    # Flibusta и некоторые другие каталоги помечают навигационные ссылки
    # пустым rel и обычным Atom-типом.
    if "application/atom+xml" in link.type and not is_acquisition_link(link):
        return link.rel not in {
            "self",
            "start",
            "up",
            "next",
            "previous",
            "first",
            "last",
            "search",
            "alternate",
            "related",
        }
    return False


def is_acquisition_link(link: FeedLink) -> bool:
    return link.rel == ACQ_REL or link.rel.startswith(ACQ_REL + "/")


def is_fb2_link(link: FeedLink) -> bool:
    if link.type in FB2_TYPES:
        return True
    path = urlparse(link.href).path.lower()
    return path.endswith(".fb2") or path.endswith(".fb2.zip")


def fb2_link(entry: FeedEntry) -> FeedLink | None:
    for link in entry.acquisition_links():
        if is_fb2_link(link):
            return link
    return None


def search_url(feed: Feed, query: str) -> str:
    """Собрать ссылку поиска источника по шаблону OpenSearch/OPDS."""
    query = (query or "").strip()
    if not query:
        return ""
    for link in feed.links:
        if link.rel != "search":
            continue
        for placeholder in ("{searchTerms}", "%7BsearchTerms%7D", "%7bsearchterms%7d"):
            if placeholder in link.href:
                return link.href.replace(placeholder, quote_plus(query))
    return ""


def is_http_url(value: str) -> bool:
    parsed = urlparse(value or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def image_link(entry: FeedEntry, *, thumbnail: bool = False) -> FeedLink | None:
    """Найти ссылку на обложку; относительные URL уже разрешены парсером."""
    rels = [
        "http://opds-spec.org/image",
        "http://opds-spec.org/cover",
        "http://opds-spec.org/image/thumbnail",
        "http://opds-spec.org/thumbnail",
        "x-stanza-cover-image",
        "x-stanza-cover-image-thumbnail",
    ]
    if thumbnail:
        rels = [
            "http://opds-spec.org/image/thumbnail",
            "http://opds-spec.org/thumbnail",
            "x-stanza-cover-image-thumbnail",
            "http://opds-spec.org/image",
        ]
    for rel in rels:
        for link in entry.links:
            if link.rel == rel and is_http_url(link.href):
                return link
    return None


def related_links(entry: FeedEntry) -> list[FeedLink]:
    return [
        link
        for link in entry.links
        if link.rel == "related" and is_http_url(link.href)
    ]


def external_entry_url(entry: FeedEntry) -> str:
    """Ссылка на страницу книги на внешнем сайте, если она есть."""
    for link in entry.links:
        if link.rel == "alternate" and "html" in link.type and is_http_url(link.href):
            return link.href
    for link in entry.links:
        if link.rel == "alternate" and is_http_url(link.href):
            return link.href
    image = image_link(entry)
    if image is not None:
        return image.href
    for link in entry.acquisition_links():
        if is_http_url(link.href):
            return link.href
    return ""


def _validate_url(url: str) -> None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OpdsError("адрес источника должен начинаться с http:// или https://")


def _auth_header(username: str, password: str) -> str:
    if not username and not password:
        return ""
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(url: str, *, username: str, password: str, accept: str) -> Request:
    _validate_url(url)
    headers = {
        "User-Agent": "KVHomeLibrary/0.8.2 (+https://localhost)",
        "Accept": accept,
    }
    auth = _auth_header(username, password)
    if auth:
        headers["Authorization"] = auth
    return Request(url, headers=headers)


def _fetch_feed_once(
    url: str,
    *,
    username: str,
    password: str,
    timeout: float,
    max_bytes: int,
) -> tuple[bytes, str]:
    request = _request(
        url,
        username=username,
        password=password,
        accept="application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "json" in content_type or "opds+json" in content_type:
                raise OpdsError("источник отдаёт OPDS 2/JSON — поддерживается только OPDS 1.2")
            data = response.read(max_bytes + 1)
            final_url = response.geturl()
    except HTTPError as exc:
        code, reason = exc.code, exc.reason
        exc.close()
        raise OpdsError(f"HTTP {code}: {reason}") from exc
    except URLError as exc:
        raise OpdsError(f"не удалось подключиться: {exc.reason}") from exc
    except TimeoutError as exc:
        raise OpdsError("источник не ответил вовремя") from exc

    if len(data) > max_bytes:
        raise OpdsError("ответ источника слишком большой")
    return data, final_url


def fetch_feed(
    url: str,
    *,
    username: str = "",
    password: str = "",
    timeout: float = 30.0,
    max_bytes: int = 8_000_000,
) -> Feed:
    """Загрузить и разобрать OPDS-фид по HTTP(S).

    При таймауте делается одна повторная попытка: некоторые каталоги
    (например, Flibusta) периодически отвечают с задержкой.
    """
    data = b""
    final_url = url
    for attempt in range(2):
        try:
            data, final_url = _fetch_feed_once(
                url,
                username=username,
                password=password,
                timeout=timeout,
                max_bytes=max_bytes,
            )
            break
        except OpdsError as exc:
            message = str(exc).lower()
            retryable = "не ответил" in message or "timed out" in message
            if attempt == 0 and retryable:
                continue
            raise

    return parse_feed(data, final_url)


def _safe_filename(name: str) -> str:
    name = _clean(name)
    name = _ILLEGAL_NAME_RE.sub("_", name).strip(" .")
    name = name[:140].rstrip(" .")
    if not name or name in {".", ".."}:
        name = "book"
    return name


def _filename_from_headers(headers: object) -> str:
    disposition = ""
    try:
        disposition = headers.get("Content-Disposition", "")  # type: ignore[attr-defined]
    except AttributeError:
        return ""
    if not disposition:
        return ""
    match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.I)
    if match:
        return match.group(1).strip()
    return ""


def _fb2_filename(name: str, content_type: str, url: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    path = urlparse(url).path.lower()
    if content_type in {"application/epub+zip", "application/pdf", "text/html"}:
        raise OpdsError("источник предлагает не FB2, а другой формат")
    if content_type in {"application/json", "application/opds+json"}:
        raise OpdsError("источник вернул JSON вместо файла книги")

    lower = name.lower()
    if lower.endswith(".fb2.zip") or lower.endswith(".fb2"):
        return name
    if lower.endswith(".zip"):
        return name[:-4] + ".fb2.zip"
    if content_type == "application/fb2+zip" or path.endswith(".fb2.zip"):
        return name + ".fb2.zip"
    return name + ".fb2"


def download_fb2(
    url: str,
    target_dir: str | Path,
    *,
    suggested_name: str = "",
    username: str = "",
    password: str = "",
    timeout: float = 30.0,
    max_bytes: int = 100_000_000,
) -> DownloadResult:
    """Скачать FB2 (или FB2.ZIP) в ``target_dir``.

    Имя файла очищается, при совпадении имён добавляется хеш. Одинаковый
    файл повторно не скачивается: возвращается уже существующий путь.
    """
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    request = _request(
        url,
        username=username,
        password=password,
        accept="application/fb2+xml,application/fb2+zip,application/octet-stream;q=0.8,*/*;q=0.5",
    )
    temp = target / f".opds-{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type in {"application/epub+zip", "application/pdf", "text/html"}:
                    raise OpdsError("источник предлагает не FB2, а другой формат")
                length = response.headers.get("Content-Length")
                if length:
                    try:
                        if int(length) > max_bytes:
                            raise OpdsError("файл слишком большой")
                    except ValueError:
                        pass
                header_name = _filename_from_headers(response.headers)
                with temp.open("wb") as handle:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise OpdsError("файл слишком большой")
                        digest.update(chunk)
                        handle.write(chunk)
        except HTTPError as exc:
            code, reason = exc.code, exc.reason
            exc.close()
            raise OpdsError(f"HTTP {code}: {reason}") from exc
        except URLError as exc:
            raise OpdsError(f"не удалось скачать файл: {exc.reason}") from exc
        except TimeoutError as exc:
            raise OpdsError("источник не ответил вовремя") from exc

        if size == 0:
            raise OpdsError("источник вернул пустой файл")

        name = _safe_filename(header_name or suggested_name or Path(urlparse(url).path).name or "book.fb2")
        name = _fb2_filename(name, content_type, url)
        final = target / name
        if final.exists():
            existing_hash = hashlib.sha256(final.read_bytes()).hexdigest()
            if existing_hash == digest.hexdigest():
                temp.unlink(missing_ok=True)
                return DownloadResult(
                    path=final,
                    size=final.stat().st_size,
                    content_type=content_type,
                    sha256=existing_hash,
                )
            suffix = "".join(final.suffixes)
            stem = final.name[: -len(suffix)] if suffix else final.name
            final = target / f"{stem}-{digest.hexdigest()[:8]}{suffix}"
        os.replace(temp, final)
        return DownloadResult(
            path=final,
            size=size,
            content_type=content_type,
            sha256=digest.hexdigest(),
        )
    finally:
        temp.unlink(missing_ok=True)
