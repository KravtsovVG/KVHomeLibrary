"""HTML-страницы каталога и администрирования.

Разметка хранится в ``templates``, стили — в ``static/style.css``.
Python-код готовит данные и вызывает шаблонизатор.
"""

from __future__ import annotations

import html as html_lib
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlencode

from .db import (    AuthorRef,
    BookRef,
    GenreRef,
    OpdsSource,
    SeriesRef,
    Stats,
    count_opds_sources,
    get_author,
    get_book,
    get_genre,
    get_opds_source,
    get_series,
    get_stats,
    list_authors,
    list_books,
    list_genres,
    list_parse_errors,
    list_series,
)
from .genres import genre_synonyms
from .fb2 import Fb2Error, parse_fb2_file
from .book_formats import format_label
from .opds_client import (
    Feed,
    FeedEntry,
    external_entry_url,
    fb2_link,
    image_link,
    related_links,
    search_url,
)
from .templating import SafeString, mark_safe, render
from . import __version__

PAGE_SIZE = 24
FILTER_LIMIT = 2000

_NOTICES = {
    "saved": "Изменения сохранены.",
    "deleted": "Книга удалена из каталога. Файл на диске остался.",
    "scanned": "Сканирование библиотеки завершено.",
    "reindexed": "Поисковый индекс перестроен.",
    "source_saved": "Источник сохранён.",
    "source_deleted": "Источник удалён.",
    "downloaded": "Книга скачана в каталог и добавлена в библиотеку.",
    "archive_scanned": "Импорт из архивов завершён.",
}
_ERRORS = {
    "book": "Книга не найдена.",
    "title": "Название книги не может быть пустым.",
    "delete": "Не удалось удалить книгу.",
    "scan": "Не удалось просканировать библиотеку. Проверьте путь и права.",
    "no_library": "Не задан каталог с книгами. Запустите сервер с --library.",
    "reindex": "Не удалось перестроить поисковый индекс.",
    "source": "Не удалось сохранить источник. Проверьте название и адрес.",
    "source_exists": "Источник с таким адресом уже существует.",
    "download": "Не удалось скачать книгу из источника.",
    "download_parse": "Файл скачан в библиотеку, но не распознан как FB2.",
    "archive_scan": "Не удалось импортировать архивы. Проверьте путь, даты и наличие 7-Zip/unrar.",
}


def _esc(value: object) -> SafeString:
    return mark_safe(html_lib.escape("" if value is None else str(value), quote=True))


def _snippet(value: str, limit: int = 180) -> str:
    value = " ".join((value or "").split())
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def _param(query: Mapping[str, list[str]], key: str, default: str = "") -> str:
    return (query.get(key, [default])[0] or "").strip()


def _int_param(query: Mapping[str, list[str]], key: str, default: int = 1) -> int:
    try:
        return max(1, int(query.get(key, [str(default)])[0]))
    except (TypeError, ValueError):
        return default


def _opt_int(query: Mapping[str, list[str]], key: str) -> int | None:
    value = _param(query, key)
    return int(value) if value.isdigit() else None


def _page_url(path: str, params: Mapping[str, object], page: int) -> str:
    clean = {key: value for key, value in params.items() if value not in (None, "")}
    if page > 1:
        clean["page"] = page
    else:
        clean.pop("page", None)
    return path + ("?" + urlencode(clean) if clean else "")


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


def _size(value: int) -> str:
    number = float(value)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ"):
        if number < 1024 or unit == "ГиБ":
            return f"{number:.1f} {unit}" if unit != "Б" else f"{int(number)} {unit}"
        number /= 1024
    return f"{value} Б"


def _layout(
    title: str,
    body: str,
    *,
    active: str = "",
    q: str = "",
    notice: str = "",
    error: str = "",
) -> bytes:
    notice_text = _NOTICES.get(notice, "")
    error_text = _ERRORS.get(error, "") if error else ""
    flash = render(
        "partials/flash.html",
        notice_text=notice_text,
        error_text=error_text,
    )
    page = render(
        "base.html",
        title=title,
        body=mark_safe(body),
        active=active,
        q=q,
        flash=mark_safe(flash),
        asset_version=__version__,
    )
    return page.encode("utf-8")


def _cover(book: BookRef) -> SafeString:
    return mark_safe(
        render(
            "partials/cover.html",
            cover={
                "image": f"/covers/{book.id}" if book.cover_path else "",
                "link_url": f"/ui/books/{book.id}",
                "title": book.title,
            },
        )
    )


def _authors(book: BookRef) -> SafeString:
    authors = [
        {"url": f"/ui/books?author_id={author.id}", "name": author.name}
        for author in book.authors
    ]
    return mark_safe(render("partials/authors.html", authors=authors))


def _genres(book: BookRef) -> SafeString:
    genres = []
    for genre in book.genres:
        synonyms = genre_synonyms(genre.code)
        genres.append(
            {
                "url": f"/ui/books?genre_id={genre.id}",
                "name": genre.name,
                "title": ("Синонимы: " + ", ".join(synonyms)) if synonyms else "",
            }
        )
    return mark_safe(render("partials/genres.html", genres=genres))


def _series(book: BookRef) -> SafeString:
    if not book.series_name:
        return mark_safe("")
    return mark_safe(
        render(
            "partials/series.html",
            series={
                "name": book.series_name,
                "number": book.series_number,
                "url": f"/ui/books?series_id={book.series_id}" if book.series_id else "",
            },
        )
    )


def _stats_html(stats: Stats) -> SafeString:
    items = [
        {"value": stats.books, "label": _books_word(stats.books)},
        {"value": stats.authors, "label": "авторов"},
        {"value": stats.genres, "label": "категорий"},
        {"value": stats.series, "label": "серий"},
        {"value": stats.failed, "label": "ошибок разбора"},
    ]
    return mark_safe(render("partials/stats.html", stats=items))


def _card(book: BookRef, *, admin: bool = False) -> dict[str, object]:
    return {
        "id": book.id,
        "url": f"/ui/books/{book.id}",
        "title": book.title,
        "cover": _cover(book),
        "authors": _authors(book),
        "series": _series(book),
        "publisher": book.publisher,
        "year": book.year,
        "annotation": _snippet(book.annotation),
        "genres": _genres(book),
        "format": book.format,
        "format_label": format_label(book.format),
        "is_fb2": book.format == "fb2",
        "admin": admin,
        "edit_url": f"/admin/books/{book.id}",
        "delete_url": f"/admin/books/{book.id}/delete",
    }


def _filters(
    conn,
    query: Mapping[str, list[str]],
    *,
    action: str,
) -> SafeString:
    q = _param(query, "q")
    author_id = _opt_int(query, "author_id")
    genre_id = _opt_int(query, "genre_id")
    series_id = _opt_int(query, "series_id")

    authors, _ = list_authors(conn, limit=FILTER_LIMIT)
    genres, _ = list_genres(conn, limit=FILTER_LIMIT)
    series, _ = list_series(conn, limit=FILTER_LIMIT)
    return mark_safe(
        render(
            "partials/filters.html",
            action=action,
            q=q,
            authors=[
                {"id": item.id, "name": item.name, "count": item.books, "selected": item.id == author_id}
                for item in authors
            ],
            genres=[
                {"id": item.id, "name": item.name, "count": item.books, "selected": item.id == genre_id}
                for item in genres
            ],
            series=[
                {"id": item.id, "name": item.name, "count": item.books, "selected": item.id == series_id}
                for item in series
            ],
        )
    )


def _pagination(
    path: str,
    params: Mapping[str, object],
    page: int,
    pages: int,
) -> SafeString:
    if pages <= 1:
        return mark_safe("")
    links = [{"url": _page_url(path, params, 1), "label": "« В начало", "current": False}]
    if page > 1:
        links.append({"url": _page_url(path, params, page - 1), "label": "← Назад", "current": False})
    links.append({"url": "", "label": f"{page} / {pages}", "current": True})
    if page < pages:
        links.append({"url": _page_url(path, params, page + 1), "label": "Вперёд →", "current": False})
    links.append({"url": _page_url(path, params, pages), "label": "В конец »", "current": False})
    return mark_safe(render("partials/pagination.html", pagination=links))


def _collection(
    title: str,
    all_url: str,
    items: Sequence[Mapping[str, object]],
    *,
    empty: str = "Пока пусто.",
) -> SafeString:
    return mark_safe(
        render(
            "partials/collection.html",
            title=title,
            all_url=all_url,
            items=items,
            empty=empty,
        )
    )


def index_page(conn, *, notice: str = "", error: str = "") -> bytes:
    stats = get_stats(conn)
    latest, _ = list_books(conn, limit=8, sort="new")
    authors, _ = list_authors(conn, limit=8)
    genres, _ = list_genres(conn, limit=10)
    series, _ = list_series(conn, limit=8)

    body = render(
        "index.html",
        stats=_stats_html(stats),
        latest=[_card(book) for book in latest],
        authors_collection=_collection(
            "Авторы",
            "/ui/authors",
            [
                {"url": f"/ui/books?author_id={item.id}", "name": item.name, "count": item.books}
                for item in authors
            ],
        ),
        genres_collection=_collection(
            "Категории",
            "/ui/genres",
            [
                {
                    "url": f"/ui/books?genre_id={item.id}",
                    "name": item.name,
                    "count": item.books,
                    "synonyms": ", ".join(genre_synonyms(item.code)),
                }
                for item in genres
            ],
        ),
        series_collection=_collection(
            "Серии",
            "/ui/series",
            [
                {"url": f"/ui/books?series_id={item.id}", "name": item.name, "count": item.books}
                for item in series
            ],
        ),
    )
    return _layout("Каталог", body, active="home", notice=notice, error=error)


def books_page(
    conn,
    query: Mapping[str, list[str]],
    *,
    admin: bool = False,
    notice: str = "",
    error: str = "",
) -> bytes:
    q = _param(query, "q")[:200]
    author_id = _opt_int(query, "author_id")
    genre_id = _opt_int(query, "genre_id")
    series_id = _opt_int(query, "series_id")
    page = _int_param(query, "page", 1)
    path = "/admin/books" if admin else "/ui/books"

    author = get_author(conn, author_id) if author_id else None
    genre = get_genre(conn, genre_id) if genre_id else None
    series = get_series(conn, series_id) if series_id else None

    books, total = list_books(
        conn,
        author_id=author.id if author else None,
        genre_id=genre.id if genre else None,
        series_id=series.id if series else None,
        query=q or None,
        offset=(page - 1) * PAGE_SIZE,
        limit=PAGE_SIZE,
    )
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        page = pages
        books, total = list_books(
            conn,
            author_id=author.id if author else None,
            genre_id=genre.id if genre else None,
            series_id=series.id if series else None,
            query=q or None,
            offset=(page - 1) * PAGE_SIZE,
            limit=PAGE_SIZE,
        )

    if author:
        title = f"Книги автора: {author.name}"
    elif genre:
        title = f"Категория: {genre.name}"
    elif series:
        title = f"Серия: {series.name}"
    elif q:
        title = f"Поиск: {q}"
    else:
        title = "Администрирование книг" if admin else "Все книги"

    params = {
        "q": q,
        "author_id": author.id if author else None,
        "genre_id": genre.id if genre else None,
        "series_id": series.id if series else None,
    }
    body = render(
        "books.html",
        title=title,
        total=total,
        word=_books_word(total),
        admin=admin,
        filters=_filters(conn, query, action=path),
        cards=[_card(book, admin=admin) for book in books],
        pagination=_pagination(path, params, page, pages),
    )
    return _layout(title, body, active="admin" if admin else "books", q=q, notice=notice, error=error)


def authors_page(conn, query: Mapping[str, list[str]], *, notice: str = "", error: str = "") -> bytes:
    page = _int_param(query, "page", 1)
    authors, total = list_authors(conn, offset=(page - 1) * 100, limit=100)
    pages = max(1, (total + 99) // 100)
    if page > pages:
        page = pages
        authors, total = list_authors(conn, offset=(page - 1) * 100, limit=100)
    body = render(
        "collection_page.html",
        title="Авторы",
        total_label=f"{total} авторов",
        items=[
            {"url": f"/ui/books?author_id={item.id}", "name": item.name, "count": item.books}
            for item in authors
        ],
        empty="Авторы не найдены.",
        pagination=_pagination("/ui/authors", {}, page, pages),
    )
    return _layout("Авторы", body, active="authors", notice=notice, error=error)


def genres_page(conn, query: Mapping[str, list[str]], *, notice: str = "", error: str = "") -> bytes:
    page = _int_param(query, "page", 1)
    genres, total = list_genres(conn, offset=(page - 1) * 100, limit=100)
    pages = max(1, (total + 99) // 100)
    if page > pages:
        page = pages
        genres, total = list_genres(conn, offset=(page - 1) * 100, limit=100)
    body = render(
        "collection_page.html",
        title="Категории",
        total_label=f"{total} категорий",
        items=[
            {
                "url": f"/ui/books?genre_id={item.id}",
                "name": item.name,
                "count": item.books,
                "synonyms": ", ".join(genre_synonyms(item.code)),
            }
            for item in genres
        ],
        empty="Категории не найдены.",
        pagination=_pagination("/ui/genres", {}, page, pages),
    )
    return _layout("Категории", body, active="genres", notice=notice, error=error)


def series_page(conn, query: Mapping[str, list[str]], *, notice: str = "", error: str = "") -> bytes:
    page = _int_param(query, "page", 1)
    series, total = list_series(conn, offset=(page - 1) * 100, limit=100)
    pages = max(1, (total + 99) // 100)
    if page > pages:
        page = pages
        series, total = list_series(conn, offset=(page - 1) * 100, limit=100)
    body = render(
        "collection_page.html",
        title="Серии",
        total_label=f"{total} серий",
        items=[
            {"url": f"/ui/books?series_id={item.id}", "name": item.name, "count": item.books}
            for item in series
        ],
        empty="Серии не найдены.",
        pagination=_pagination("/ui/series", {}, page, pages),
    )
    return _layout("Серии", body, active="series", notice=notice, error=error)


def book_page(
    conn,
    book_id: int,
    *,
    admin: bool = False,
    notice: str = "",
    error: str = "",
) -> bytes | None:
    book = get_book(conn, book_id)
    if book is None:
        return None

    meta = [
        {"label": "Авторы", "html": _authors(book)},
        {"label": "Серия", "html": _series(book) or _esc("—")},
        {"label": "Год", "text": book.year or "—"},
        {"label": "Издательство", "text": book.publisher or "—"},
        {"label": "Город", "text": book.city or "—"},
        {"label": "ISBN", "text": book.isbn or "—"},
        {"label": "Язык", "text": book.lang or "—"},
        {"label": "Категории", "html": _genres(book) or _esc("—")},
        {"label": "Формат", "text": format_label(book.format)},
        {
            "label": "Файл",
            "url": f"/download/{book.id}",
            "text": f"{book.file_name} · {_size(book.file_size)}",
        },
        {"label": "Добавлена", "text": book.added_at or "—"},
        {"label": "Обновлена", "text": book.updated_at or "—"},
    ]
    if admin:
        meta.append({"label": "Путь", "text": book.file_path})
        meta.append({"label": "Документ FB2", "text": book.doc_id or "—"})

    body = render(
        "book.html",
        book={
            "id": book.id,
            "title": book.title,
            "authors": _authors(book),
            "cover": _cover(book),
            "meta": meta,
            "annotation": book.annotation,
            "format_label": format_label(book.format),
            "is_fb2": book.format == "fb2",
            "admin": admin,
            "edit_url": f"/admin/books/{book.id}",
            "delete_url": f"/admin/books/{book.id}/delete",
            "back_url": "/admin/books" if admin else "/ui/books",
        },
    )
    return _layout(book.title, body, active="admin" if admin else "books", notice=notice, error=error)


def _edit_book(book: BookRef) -> dict[str, object]:
    return {
        "id": book.id,
        "title": book.title,
        "annotation": book.annotation,
        "file_name": book.file_name,
        "authors_text": ", ".join(author.name for author in book.authors),
        "genres_text": ", ".join(genre.code for genre in book.genres),
        "series_name": book.series_name,
        "series_number": book.series_number,
        "lang": book.lang,
        "year": book.year,
        "publisher": book.publisher,
        "city": book.city,
        "isbn": book.isbn,
    }


def edit_page(
    conn,
    book_id: int,
    *,
    notice: str = "",
    error: str = "",
) -> bytes | None:
    book = get_book(conn, book_id)
    if book is None:
        return None
    body = render("edit.html", book=_edit_book(book))
    return _layout("Редактирование", body, active="admin", notice=notice, error=error)


def edit_modal(conn, book_id: int) -> bytes | None:
    book = get_book(conn, book_id)
    if book is None:
        return None
    return render("edit_modal.html", book=_edit_book(book)).encode("utf-8")


def admin_page(
    conn,
    query: Mapping[str, list[str]],
    *,
    library_path: Path | None,
    auth_enabled: bool,
    notice: str = "",
    error: str = "",
) -> bytes:
    stats = get_stats(conn)
    errors = [dict(row) for row in list_parse_errors(conn, limit=20)]
    archive_summary = (query.get("archive_summary", [""])[0] or "").strip()[:2000]
    body = render(
        "admin.html",
        stats=_stats_html(stats),
        last_scan=stats.last_scan or "—",
        auth_enabled=auth_enabled,
        library_path=str(library_path) if library_path else "",
        sources_count=count_opds_sources(conn),
        errors=errors,
        archive_summary=archive_summary,
    )
    return _layout("Администрирование", body, active="admin", notice=notice, error=error)


def sources_page(
    conn,
    query: Mapping[str, list[str]],
    *,
    notice: str = "",
    error: str = "",
) -> bytes:
    from .db import list_opds_sources

    sources = []
    for source in list_opds_sources(conn):
        sources.append(
            {
                "id": source.id,
                "name": source.name,
                "url": source.url,
                "status": "включён" if source.enabled else "выключен",
                "last_checked_at": source.last_checked_at or "—",
                "last_error": source.last_error,
                "open_url": _opds_url(source.id, source.url),
                "edit_url": f"/admin/sources/{source.id}",
                "delete_url": f"/admin/sources/{source.id}/delete",
            }
        )
    body = render("sources.html", sources=sources)
    return _layout("Источники OPDS", body, active="admin", notice=notice, error=error)


def source_edit_page(
    conn,
    source_id: int,
    *,
    notice: str = "",
    error: str = "",
) -> bytes | None:
    source = get_opds_source(conn, source_id)
    if source is None:
        return None
    body = render(
        "source_edit.html",
        source={
            "id": source.id,
            "name": source.name,
            "url": source.url,
            "username": source.username,
            "enabled": source.enabled,
            "last_checked_at": source.last_checked_at or "—",
            "last_error": source.last_error or "нет",
            "open_url": _opds_url(source.id, source.url),
            "action": f"/admin/sources/{source.id}",
            "delete_url": f"/admin/sources/{source.id}/delete",
        },
    )
    return _layout("Источник OPDS", body, active="admin", notice=notice, error=error)


def _opds_url(source_id: int, url: str) -> str:
    return "/admin/opds?" + urlencode({"source_id": source_id, "url": url})


def source_feed_page(
    source: OpdsSource,
    feed: Feed,
    current_url: str,
    *,
    notice: str = "",
    error: str = "",
) -> bytes:
    navigation = [
        {
            "url": _opds_url(source.id, entry.navigation_link().href),
            "title": entry.title,
        }
        for entry in feed.entries
        if entry.navigation_link() is not None
    ]

    entries = []
    for entry in feed.entries:
        if not entry.acquisition_links() and not (
            related_links(entry) and entry.navigation_link() is None
        ):
            continue

        link = fb2_link(entry)
        series_html = mark_safe("")
        if entry.series:
            remote_search = search_url(feed, entry.series)
            series_html = mark_safe(
                render(
                    "partials/series.html",
                    series={
                        "name": entry.series,
                        "number": entry.series_number,
                        "url": _opds_url(source.id, remote_search) if remote_search else "",
                    },
                )
            )

        related = []
        for related_link in related_links(entry):
            if "atom" in related_link.type:
                url = _opds_url(source.id, related_link.href)
                external = False
            else:
                url = related_link.href
                external = True
            related.append(
                {
                    "url": url,
                    "title": related_link.title or "Связанный раздел",
                    "external": external,
                }
            )

        cover = image_link(entry)
        entries.append(
            {
                "title": entry.title,
                "authors": ", ".join(entry.authors) or "Автор не указан",
                "series": series_html,
                "summary": _snippet(entry.summary, 2000),
                "summary_html": mark_safe(entry.summary_html),
                "categories": entry.categories,
                "cover_image": cover.href if cover is not None else "",
                "external_url": external_entry_url(entry) or (cover.href if cover is not None else ""),
                "related": related,
                "fb2_href": link.href if link is not None else "",
                "source_id": source.id,
                "return_url": current_url,
            }
        )

    pager_links = []
    for rel, label in (
        ("first", "« В начало"),
        ("previous", "← Назад"),
        ("next", "Вперёд →"),
        ("last", "В конец »"),
    ):
        pager_link = feed.link_by_rel(rel)
        if pager_link is not None:
            pager_links.append(
                {"url": _opds_url(source.id, pager_link.href), "label": label, "current": False}
            )
    pager = mark_safe(render("partials/pagination.html", pagination=pager_links))

    body = render(
        "source_feed.html",
        feed_title=feed.title,
        source_name=source.name,
        current_url=current_url,
        refresh_url=_opds_url(source.id, current_url),
        nav_entries=navigation,
        entries=entries,
        pager=pager,
    )
    return _layout(feed.title, body, active="admin", notice=notice, error=error)


def opds_error_page(source: OpdsSource, url: str, message: str) -> bytes:
    body = render(
        "opds_error.html",
        source_name=source.name,
        message=message,
        url=url,
        retry_url=_opds_url(source.id, url),
    )
    return _layout("Ошибка OPDS", body, active="admin")


def reader_page(conn, book_id: int) -> bytes | None:
    book = get_book(conn, book_id)
    if book is None:
        return None
    if book.format != "fb2":
        return reader_error_page(
            book,
            f"Встроенная читалка поддерживает только FB2. Формат книги: {format_label(book.format)}.",
        )
    try:
        parsed = parse_fb2_file(book.file_path)
    except Fb2Error as exc:
        return reader_error_page(book, str(exc))
    authors = ", ".join(author.name for author in book.authors) or "Автор не указан"
    body = render(
        "reader.html",
        book={"id": book.id, "title": book.title, "authors": authors},
        content=mark_safe(parsed.content_html),
        toc=parsed.toc,
        asset_version=__version__,
    )
    return body.encode("utf-8")


def reader_error_page(book: BookRef, message: str) -> bytes:
    body = render(
        "reader_error.html",
        book={"id": book.id, "title": book.title},
        message=message,
        asset_version=__version__,
    )
    return body.encode("utf-8")


def admin_forbidden_page() -> bytes:
    body = render("forbidden.html")
    return _layout("Доступ запрещён", body, active="admin")
