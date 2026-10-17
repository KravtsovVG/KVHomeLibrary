"""Формирование OPDS 1.2 каталога.

Каталог разделён на навигационные ветки:

* ``/opds`` — корень;
* ``/opds/authors`` — авторы;
* ``/opds/genres`` — категории (жанры);
* ``/opds/series`` — серии;
* ``/opds/books`` — все книги.

Внутри каждой ветки книги отдаются acquisition-фидами с ссылками на
скачивание FB2 и обложки.
"""

from __future__ import annotations

import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping
from urllib.parse import urlencode

import xml.etree.ElementTree as ET

from . import __version__
from .db import (
    AuthorRef,
    BookRef,
    GenreRef,
    SeriesRef,
    get_stats,
)
from .genres import genre_name

ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/terms/"
OPDS_NS = "http://opds-spec.org/2010/catalog"
OS_NS = "http://a9.com/-/spec/opensearch/1.1/"

NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
FB2_TYPE = "application/fb2+xml"
FB2_ZIP_TYPE = "application/fb2+zip"

ET.register_namespace("", ATOM_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("opds", OPDS_NS)
ET.register_namespace("os", OS_NS)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _feed_id(url: str) -> str:
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'kvhomelibrary:' + url)}"


def _book_id(book_id: int) -> str:
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'kvhomelibrary:book:{book_id}')}"


def _element(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    elem = ET.SubElement(parent, tag, {key: value for key, value in attrs.items() if value})
    if text:
        elem.text = text
    return elem


def _link(parent: ET.Element, rel: str, href: str, type_: str = "", title: str = "") -> None:
    attrs = {"rel": rel, "href": href}
    if type_:
        attrs["type"] = type_
    if title:
        attrs["title"] = title
    ET.SubElement(parent, f"{{{ATOM_NS}}}link", attrs)


def _books_word(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return "книг"
    last = count % 10
    if last == 1:
        return "книга"
    if 2 <= last <= 4:
        return "книги"
    return "книг"


def _page_url(base_url: str, path: str, params: Mapping[str, object], page: int) -> str:
    query: dict[str, object] = {
        key: value for key, value in params.items() if value not in (None, "")
    }
    if page > 1:
        query["page"] = page
    else:
        query.pop("page", None)
    url = _url(base_url, path)
    if query:
        url += "?" + urlencode(query)
    return url


def _page_state(total: int, requested: int, page_size: int) -> tuple[int, int, int]:
    page_size = max(1, page_size)
    pages = max(1, math.ceil(total / page_size))
    page = min(max(1, requested), pages)
    return page, pages, (page - 1) * page_size


def _add_pagination(
    feed: ET.Element,
    base_url: str,
    path: str,
    params: Mapping[str, object],
    page: int,
    pages: int,
) -> None:
    if pages > 1:
        _link(feed, "first", _page_url(base_url, path, params, 1))
        _link(feed, "last", _page_url(base_url, path, params, pages))
        if page > 1:
            _link(feed, "previous", _page_url(base_url, path, params, page - 1))
        if page < pages:
            _link(feed, "next", _page_url(base_url, path, params, page + 1))


def _new_feed(
    title: str,
    self_url: str,
    *,
    kind: str,
    start_url: str,
    up_url: str = "",
    search_url: str = "",
    subtitle: str = "",
) -> ET.Element:
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    _element(feed, f"{{{ATOM_NS}}}id", _feed_id(self_url))
    _element(feed, f"{{{ATOM_NS}}}title", title)
    _element(feed, f"{{{ATOM_NS}}}updated", _now())
    author = ET.SubElement(feed, f"{{{ATOM_NS}}}author")
    _element(author, f"{{{ATOM_NS}}}name", "KVHomeLibrary")
    _link(feed, "self", self_url, kind)
    _link(feed, "start", start_url, NAV_TYPE)
    if up_url:
        _link(feed, "up", up_url, NAV_TYPE)
    if search_url:
        _link(feed, "search", search_url, "application/opensearchdescription+xml")
    if subtitle:
        _element(feed, f"{{{ATOM_NS}}}subtitle", subtitle)
    return feed


def _nav_entry(
    feed: ET.Element,
    *,
    title: str,
    href: str,
    seed: str,
    content: str,
    updated: str = "",
    kind: str = NAV_TYPE,
) -> ET.Element:
    entry = ET.SubElement(feed, f"{{{ATOM_NS}}}entry")
    _element(entry, f"{{{ATOM_NS}}}title", title)
    _element(entry, f"{{{ATOM_NS}}}id", _feed_id(seed))
    _element(entry, f"{{{ATOM_NS}}}updated", updated or _now())
    if content:
        _element(entry, f"{{{ATOM_NS}}}content", content, type="text")
    _link(entry, "subsection", href, kind)
    return entry


def _book_entry(feed: ET.Element, book: BookRef, base_url: str) -> ET.Element:
    entry = ET.SubElement(feed, f"{{{ATOM_NS}}}entry")
    _element(entry, f"{{{ATOM_NS}}}title", book.title)
    _element(entry, f"{{{ATOM_NS}}}id", _book_id(book.id))
    _element(entry, f"{{{ATOM_NS}}}updated", book.updated_at or _now())

    authors = book.authors or [AuthorRef(id=0, name="Неизвестный автор")]
    for author in authors:
        author_elem = ET.SubElement(entry, f"{{{ATOM_NS}}}author")
        _element(author_elem, f"{{{ATOM_NS}}}name", author.name)
        if author.id:
            _element(
                author_elem,
                f"{{{ATOM_NS}}}uri",
                _url(base_url, f"/opds/authors/{author.id}"),
            )

    if book.annotation:
        text = book.annotation
        if len(text) > 3000:
            text = text[:2997].rstrip() + "…"
        _element(entry, f"{{{ATOM_NS}}}content", text, type="text")

    for genre in book.genres:
        _element(
            entry,
            f"{{{ATOM_NS}}}category",
            term=genre.code,
            label=genre.name or genre_name(genre.code),
        )

    if book.lang:
        _element(entry, f"{{{DC_NS}}}language", book.lang)
    if book.year:
        _element(entry, f"{{{DC_NS}}}issued", book.year)
    if book.publisher:
        _element(entry, f"{{{DC_NS}}}publisher", book.publisher)
    if book.isbn:
        _element(entry, f"{{{DC_NS}}}identifier", f"urn:isbn:{book.isbn}")
    if book.series_name:
        _element(entry, f"{{{DC_NS}}}isPartOf", book.series_name)
        ET.SubElement(
            entry,
            f"{{{OPDS_NS}}}series",
            {
                "name": book.series_name,
                **({"number": book.series_number} if book.series_number else {}),
            },
        )

    if book.cover_path:
        mime = book.cover_mime or "image/jpeg"
        cover_url = _url(base_url, f"/covers/{book.id}")
        _link(entry, "http://opds-spec.org/image", cover_url, mime)
        _link(entry, "http://opds-spec.org/image/thumbnail", cover_url, mime)

    file_type = FB2_ZIP_TYPE if book.is_zip else FB2_TYPE
    attrs = {
        "rel": "http://opds-spec.org/acquisition",
        "href": _url(base_url, f"/download/{book.id}"),
        "type": file_type,
    }
    if book.file_size:
        attrs["length"] = str(book.file_size)
    ET.SubElement(entry, f"{{{ATOM_NS}}}link", attrs)

    if book.series_id:
        _link(
            entry,
            "related",
            _url(base_url, f"/opds/series/{book.series_id}"),
            ACQ_TYPE,
            f"Все книги серии: {book.series_name}",
        )
    return entry


def root_feed(conn: sqlite3.Connection, base_url: str) -> bytes:
    stats = get_stats(conn)
    start = _url(base_url, "/opds")
    self_url = _url(base_url, "/opds")
    search = _url(base_url, "/opds/opensearch.xml")
    feed = _new_feed(
        "KVHomeLibrary",
        self_url,
        kind=NAV_TYPE,
        start_url=start,
        search_url=search,
        subtitle=f"Домашняя библиотека: {stats.books} {_books_word(stats.books)}",
    )

    _nav_entry(
        feed,
        title="Авторы",
        href=_url(base_url, "/opds/authors"),
        seed=start + "#authors",
        content=f"Книги по авторам — {stats.authors}",
    )
    _nav_entry(
        feed,
        title="Категории",
        href=_url(base_url, "/opds/genres"),
        seed=start + "#genres",
        content=f"Книги по жанрам — {stats.genres}",
    )
    _nav_entry(
        feed,
        title="Серии",
        href=_url(base_url, "/opds/series"),
        seed=start + "#series",
        content=f"Книги по сериям — {stats.series}",
    )
    _nav_entry(
        feed,
        title="Все книги",
        href=_url(base_url, "/opds/books"),
        seed=start + "#books",
        content=f"Весь каталог — {stats.books} {_books_word(stats.books)}",
        kind=ACQ_TYPE,
    )
    return to_xml(feed)


def authors_feed(
    conn: sqlite3.Connection,
    base_url: str,
    *,
    page: int = 1,
    page_size: int = 100,
    total: int,
    authors: Iterable[AuthorRef],
) -> bytes:
    start = _url(base_url, "/opds")
    page, pages, _ = _page_state(total, page, page_size)
    self_url = _page_url(base_url, "/opds/authors", {}, page)
    feed = _new_feed(
        "Авторы",
        self_url,
        kind=NAV_TYPE,
        start_url=start,
        up_url=start,
        search_url=_url(base_url, "/opds/opensearch.xml"),
        subtitle=f"Всего авторов: {total}",
    )
    for author in authors:
        _nav_entry(
            feed,
            title=author.name,
            href=_url(base_url, f"/opds/authors/{author.id}"),
            seed=f"{base_url}/opds/authors/{author.id}",
            content=f"{author.books} {_books_word(author.books)}",
            kind=ACQ_TYPE,
        )
    _add_pagination(feed, base_url, "/opds/authors", {}, page, pages)
    return to_xml(feed)


def genres_feed(
    conn: sqlite3.Connection,
    base_url: str,
    *,
    page: int = 1,
    page_size: int = 100,
    total: int,
    genres: Iterable[GenreRef],
) -> bytes:
    start = _url(base_url, "/opds")
    page, pages, _ = _page_state(total, page, page_size)
    self_url = _page_url(base_url, "/opds/genres", {}, page)
    feed = _new_feed(
        "Категории",
        self_url,
        kind=NAV_TYPE,
        start_url=start,
        up_url=start,
        search_url=_url(base_url, "/opds/opensearch.xml"),
        subtitle=f"Всего категорий: {total}",
    )
    for genre in genres:
        _nav_entry(
            feed,
            title=genre.name or genre_name(genre.code),
            href=_url(base_url, f"/opds/genres/{genre.id}"),
            seed=f"{base_url}/opds/genres/{genre.id}",
            content=f"{genre.books} {_books_word(genre.books)} · {genre.code}",
            kind=ACQ_TYPE,
        )
    _add_pagination(feed, base_url, "/opds/genres", {}, page, pages)
    return to_xml(feed)


def series_feed(
    conn: sqlite3.Connection,
    base_url: str,
    *,
    page: int = 1,
    page_size: int = 100,
    total: int,
    series: Iterable[SeriesRef],
) -> bytes:
    start = _url(base_url, "/opds")
    page, pages, _ = _page_state(total, page, page_size)
    self_url = _page_url(base_url, "/opds/series", {}, page)
    feed = _new_feed(
        "Серии",
        self_url,
        kind=NAV_TYPE,
        start_url=start,
        up_url=start,
        search_url=_url(base_url, "/opds/opensearch.xml"),
        subtitle=f"Всего серий: {total}",
    )
    for item in series:
        _nav_entry(
            feed,
            title=item.name,
            href=_url(base_url, f"/opds/series/{item.id}"),
            seed=f"{base_url}/opds/series/{item.id}",
            content=f"{item.books} {_books_word(item.books)}",
            kind=ACQ_TYPE,
        )
    _add_pagination(feed, base_url, "/opds/series", {}, page, pages)
    return to_xml(feed)


def books_feed(
    conn: sqlite3.Connection,
    base_url: str,
    *,
    page: int = 1,
    page_size: int = 50,
    total: int,
    books: Iterable[BookRef],
    title: str = "Все книги",
    path: str = "/opds/books",
    up_url: str = "",
    params: Mapping[str, object] | None = None,
    subtitle: str = "",
) -> bytes:
    start = _url(base_url, "/opds")
    page, pages, _ = _page_state(total, page, page_size)
    link_params = dict(params or {})
    self_url = _page_url(base_url, path, link_params, page)
    feed = _new_feed(
        title,
        self_url,
        kind=ACQ_TYPE,
        start_url=start,
        up_url=up_url or _url(base_url, "/opds"),
        search_url=_url(base_url, "/opds/opensearch.xml"),
        subtitle=subtitle or f"Книг: {total}",
    )
    for book in books:
        _book_entry(feed, book, base_url)
    _add_pagination(feed, base_url, path, link_params, page, pages)
    return to_xml(feed)


def author_books_feed(
    conn: sqlite3.Connection,
    base_url: str,
    author: AuthorRef,
    *,
    page: int,
    page_size: int,
    total: int,
    books: Iterable[BookRef],
) -> bytes:
    return books_feed(
        conn,
        base_url,
        page=page,
        page_size=page_size,
        total=total,
        books=books,
        title=author.name,
        path=f"/opds/authors/{author.id}",
        up_url=_url(base_url, "/opds/authors"),
        subtitle=f"Книги автора · {total} {_books_word(total)}",
    )


def genre_books_feed(
    conn: sqlite3.Connection,
    base_url: str,
    genre: GenreRef,
    *,
    page: int,
    page_size: int,
    total: int,
    books: Iterable[BookRef],
) -> bytes:
    return books_feed(
        conn,
        base_url,
        page=page,
        page_size=page_size,
        total=total,
        books=books,
        title=genre.name or genre_name(genre.code),
        path=f"/opds/genres/{genre.id}",
        up_url=_url(base_url, "/opds/genres"),
        subtitle=f"Категория · {total} {_books_word(total)}",
    )


def series_books_feed(
    conn: sqlite3.Connection,
    base_url: str,
    series: SeriesRef,
    *,
    page: int,
    page_size: int,
    total: int,
    books: Iterable[BookRef],
) -> bytes:
    return books_feed(
        conn,
        base_url,
        page=page,
        page_size=page_size,
        total=total,
        books=books,
        title=series.name,
        path=f"/opds/series/{series.id}",
        up_url=_url(base_url, "/opds/series"),
        subtitle=f"Серия · {total} {_books_word(total)}",
        params={},
    )


def search_feed(
    conn: sqlite3.Connection,
    base_url: str,
    query: str,
    *,
    page: int,
    page_size: int,
    total: int,
    books: Iterable[BookRef],
) -> bytes:
    return books_feed(
        conn,
        base_url,
        page=page,
        page_size=page_size,
        total=total,
        books=books,
        title=f"Поиск: {query}",
        path="/opds/search",
        up_url=_url(base_url, "/opds"),
        params={"q": query},
        subtitle=f"Найдено: {total} {_books_word(total)}",
    )


def single_book_feed(
    conn: sqlite3.Connection,
    base_url: str,
    book: BookRef,
) -> bytes:
    feed = _new_feed(
        book.title,
        _url(base_url, f"/opds/books/{book.id}"),
        kind=ACQ_TYPE,
        start_url=_url(base_url, "/opds"),
        up_url=_url(base_url, "/opds/books"),
        search_url=_url(base_url, "/opds/opensearch.xml"),
        subtitle="Карточка книги",
    )
    _book_entry(feed, book, base_url)
    return to_xml(feed)


def opensearch_xml(base_url: str) -> bytes:
    template = _url(base_url, "/opds/search") + "?q={searchTerms}"
    root = ET.Element(f"{{{OS_NS}}}OpenSearchDescription")
    _element(root, f"{{{OS_NS}}}ShortName", "KVHomeLibrary")
    _element(
        root,
        f"{{{OS_NS}}}Description",
        "Поиск книг в домашней библиотеке по названию, автору, серии и аннотации",
    )
    _element(root, f"{{{OS_NS}}}InputEncoding", "UTF-8")
    _element(root, f"{{{OS_NS}}}OutputEncoding", "UTF-8")
    ET.SubElement(
        root,
        f"{{{OS_NS}}}Url",
        {"type": ACQ_TYPE, "template": template},
    )
    return to_xml(root)


def to_xml(root: ET.Element) -> bytes:
    ET.indent(root, space="  ")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if not data.endswith(b"\n"):
        data += b"\n"
    return data


def version() -> str:
    return __version__
