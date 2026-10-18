"""HTTP-сервер с OPDS-каталогом и выдачей файлов книг."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
import socket
import sqlite3
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from . import __version__, web
from .archive_scanner import ArchiveError, scan_archives
from .book_formats import format_mime, read_book_payload
from .db import (
    connect_db,
    count_authors,
    count_books,
    count_genres,
    count_series,
    create_opds_source,
    delete_books,
    delete_opds_source,
    get_author,
    get_book,
    get_genre,
    get_opds_source,
    get_series,
    get_stats,
    init_db,
    list_authors,
    list_books,
    list_genres,
    list_series,
    mark_opds_source,
    record_parse_error,
    reindex_fts,
    update_book_metadata,
    update_opds_source,
    upsert_book,
)
from .fb2 import Fb2Error, parse_fb2_file
from .opds import (
    ACQ_TYPE,
    NAV_TYPE,
    author_books_feed,
    authors_feed,
    books_feed,
    genre_books_feed,
    genres_feed,
    opensearch_xml,
    root_feed,
    search_feed,
    series_books_feed,
    series_feed,
    single_book_feed,
)
from .opds_client import OpdsError, download_fb2, fetch_feed
from .scanner import scan_library

LOGGER = logging.getLogger("kvhomelibrary.server")

_BOOK_PATH_RE = re.compile(r"^/opds/books/(\d+)$")
_AUTHOR_PATH_RE = re.compile(r"^/opds/authors/(\d+)$")
_GENRE_PATH_RE = re.compile(r"^/opds/genres/(\d+)$")
_SERIES_PATH_RE = re.compile(r"^/opds/series/(\d+)$")
_DOWNLOAD_PATH_RE = re.compile(r"^/download/(\d+)$")
_COVER_PATH_RE = re.compile(r"^/covers/(\d+)$")
_UI_BOOK_PATH_RE = re.compile(r"^/ui/books/(\d+)$")
_READ_PATH_RE = re.compile(r"^/read/(\d+)$")
_ADMIN_BOOK_PATH_RE = re.compile(r"^/admin/books/(\d+)$")
_ADMIN_DELETE_PATH_RE = re.compile(r"^/admin/books/(\d+)/delete$")
_ADMIN_SOURCE_PATH_RE = re.compile(r"^/admin/sources/(\d+)$")
_ADMIN_SOURCE_DELETE_PATH_RE = re.compile(r"^/admin/sources/(\d+)/delete$")


@dataclass(slots=True)
class ServerConfig:
    db_path: Path
    covers_dir: Path
    page_size: int = 50
    public_url: str = ""
    library_path: Path | None = None
    admin_password: str = ""


def _int_param(query: Mapping[str, list[str]], key: str, default: int) -> int:
    try:
        return int(query.get(key, [str(default)])[0])
    except (TypeError, ValueError):
        return default


def _page_count(total: int, page_size: int) -> int:
    page_size = max(1, page_size)
    return max(1, (total + page_size - 1) // page_size)


class OPDSRequestHandler(BaseHTTPRequestHandler):
    server_version = f"KVHomeLibrary/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def config(self) -> ServerConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(head=True)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path != "/":
            path = path.rstrip("/")
        if not path.startswith("/admin"):
            self._send_text("Метод не поддерживается", 405)
            return

        conn = connect_db(self.config.db_path)
        try:
            if not self._require_admin(head=False):
                return
            if not self._same_origin():
                self._send_text("Запрос отклонён: неверный Origin", 403)
                return
            form = self._read_post_form()
            if form is None:
                return
            self._admin_post(conn, path, form)
        finally:
            conn.close()

    def _read_post_form(self) -> dict[str, list[str]] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "application/x-www-form-urlencoded":
            self._send_text("Ожидается application/x-www-form-urlencoded", 415)
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_text("Неверный Content-Length", 400)
            return None
        if length > 2_000_000:
            self._send_text("Слишком большой запрос", 413)
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._send_text("Ожидается UTF-8", 400)
            return None
        return parse_qs(text, keep_blank_values=True)

    def _is_local_client(self) -> bool:
        address = str(self.client_address[0])
        if (
            address == "::1"
            or address.startswith("127.")
            or address.startswith("::ffff:127.")
        ):
            return True

        local_addresses: set[str] = set()
        try:
            local_addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                local_addresses.add(sock.getsockname()[0])
        except OSError:
            pass
        return address.removeprefix("::ffff:") in local_addresses

    def _require_admin(self, *, head: bool) -> bool:
        password = self.config.admin_password
        if not password:
            if self._is_local_client():
                return True
            self._send_bytes(
                web.admin_forbidden_page(),
                "text/html; charset=utf-8",
                status=403,
                head=head,
            )
            return False

        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                decoded = ""
            _, _, supplied = decoded.partition(":")
            if hmac.compare_digest(supplied, password):
                return True

        self._send_bytes(
            "Требуется пароль администратора.".encode("utf-8"),
            "text/plain; charset=utf-8",
            status=401,
            head=head,
            extra_headers={
                "WWW-Authenticate": 'Basic realm="KVHomeLibrary admin", charset="UTF-8"'
            },
        )
        return False

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc.lower() == host.lower()

    def _handle(self, *, head: bool) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path != "/":
            path = path.rstrip("/")
        query = parse_qs(parsed.query, keep_blank_values=True)
        base_url = self._base_url()
        page = max(1, _int_param(query, "page", 1))
        page_size = min(max(1, self.config.page_size), 500)

        conn = connect_db(self.config.db_path)
        try:
            if path == "/ui" or path.startswith("/ui/"):
                self._ui_route(conn, path, query, head=head)
            elif path == "/admin" or path.startswith("/admin/"):
                if self._require_admin(head=head):
                    self._admin_route(conn, path, query, head=head)
            else:
                self._route(
                    conn,
                    path,
                    query,
                    base_url=base_url,
                    page=page,
                    page_size=page_size,
                    head=head,
                )
        finally:
            conn.close()

    def _send_html(self, body: bytes, *, head: bool = False) -> None:
        self._send_bytes(body, "text/html; charset=utf-8", head=head)

    def _wants_json(self) -> bool:
        return (
            self.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in self.headers.get("Accept", "")
        )

    def _send_json(
        self,
        payload: Mapping[str, object],
        *,
        status: int = 200,
        head: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            body,
            "application/json; charset=utf-8",
            status=status,
            head=head,
        )

    def _ui_route(
        self,
        conn,
        path: str,
        query: Mapping[str, list[str]],
        *,
        head: bool,
    ) -> None:
        notice = query.get("notice", [""])[0]
        error = query.get("error", [""])[0]
        if path == "/ui":
            body = web.index_page(conn, notice=notice, error=error)
        elif path == "/ui/books":
            body = web.books_page(conn, query, notice=notice, error=error)
        elif path == "/ui/authors":
            body = web.authors_page(conn, query, notice=notice, error=error)
        elif path == "/ui/genres":
            body = web.genres_page(conn, query, notice=notice, error=error)
        elif path == "/ui/series":
            body = web.series_page(conn, query, notice=notice, error=error)
        elif _UI_BOOK_PATH_RE.match(path):
            book_id = int(_UI_BOOK_PATH_RE.match(path).group(1))  # type: ignore[union-attr]
            body = web.book_page(conn, book_id, notice=notice, error=error)
            if body is None:
                self._send_text("Книга не найдена", 404, head=head)
                return
        else:
            self._send_text("Страница не найдена", 404, head=head)
            return
        self._send_html(body, head=head)

    def _admin_route(
        self,
        conn,
        path: str,
        query: Mapping[str, list[str]],
        *,
        head: bool,
    ) -> None:
        notice = query.get("notice", [""])[0]
        error = query.get("error", [""])[0]
        if path == "/admin":
            body = web.admin_page(
                conn,
                query,
                library_path=self.config.library_path,
                auth_enabled=bool(self.config.admin_password),
                notice=notice,
                error=error,
            )
        elif path == "/admin/books":
            body = web.books_page(
                conn, query, admin=True, notice=notice, error=error
            )
        elif path == "/admin/sources":
            body = web.sources_page(conn, query, notice=notice, error=error)
        elif path == "/admin/opds":
            self._admin_opds(conn, query, head=head)
            return
        elif _ADMIN_SOURCE_PATH_RE.match(path):
            source_id = int(_ADMIN_SOURCE_PATH_RE.match(path).group(1))  # type: ignore[union-attr]
            body = web.source_edit_page(
                conn, source_id, notice=notice, error=error
            )
            if body is None:
                self._send_text("Источник не найден", 404, head=head)
                return
        elif _ADMIN_BOOK_PATH_RE.match(path):
            book_id = int(_ADMIN_BOOK_PATH_RE.match(path).group(1))  # type: ignore[union-attr]
            if query.get("ajax", [""])[0] == "1" or self._wants_json():
                modal = web.edit_modal(conn, book_id)
                if modal is None:
                    self._send_text("Книга не найдена", 404, head=head)
                    return
                self._send_html(modal, head=head)
                return
            body = web.edit_page(conn, book_id, notice=notice, error=error)
            if body is None:
                self._send_text("Книга не найдена", 404, head=head)
                return
        else:
            self._send_text("Страница не найдена", 404, head=head)
            return
        self._send_html(body, head=head)

    def _admin_opds(
        self,
        conn,
        query: Mapping[str, list[str]],
        *,
        head: bool,
    ) -> None:
        try:
            source_id = int(query.get("source_id", ["0"])[0])
        except (TypeError, ValueError):
            source_id = 0
        source = get_opds_source(conn, source_id)
        if source is None:
            self._send_text("Источник не найден", 404, head=head)
            return

        url = (query.get("url", [source.url])[0] or "").strip()[:2000] or source.url
        notice = query.get("notice", [""])[0]
        error = query.get("error", [""])[0]
        try:
            feed = fetch_feed(
                url,
                username=source.username,
                password=source.password,
            )
        except OpdsError as exc:
            mark_opds_source(conn, source.id, error=str(exc))
            self._send_html(
                web.opds_error_page(source, url, str(exc)), head=head
            )
            return

        mark_opds_source(conn, source.id, error="")
        self._send_html(
            web.source_feed_page(
                source,
                feed,
                feed.url,
                notice=notice,
                error=error,
            ),
            head=head,
        )

    def _admin_post(
        self,
        conn,
        path: str,
        form: Mapping[str, list[str]],
    ) -> None:
        def value(name: str) -> str:
            return (form.get(name, [""])[0] or "").strip()

        source_delete_match = _ADMIN_SOURCE_DELETE_PATH_RE.match(path)
        if source_delete_match:
            source_id = int(source_delete_match.group(1))
            if delete_opds_source(conn, source_id):
                self._redirect("/admin/sources?notice=source_deleted", 303)
            else:
                self._redirect("/admin/sources?error=source", 303)
            return

        source_match = _ADMIN_SOURCE_PATH_RE.match(path)
        if source_match:
            source_id = int(source_match.group(1))
            if get_opds_source(conn, source_id) is None:
                self._send_text("Источник не найден", 404)
                return
            password: str | None = None
            if "clear_password" in form:
                password = ""
            elif value("password"):
                password = value("password")
            try:
                update_opds_source(
                    conn,
                    source_id,
                    name=value("name"),
                    url=value("url"),
                    username=value("username"),
                    password=password,
                    enabled="enabled" in form,
                )
            except sqlite3.IntegrityError:
                self._redirect(f"/admin/sources/{source_id}?error=source_exists", 303)
                return
            except ValueError:
                self._redirect(f"/admin/sources/{source_id}?error=source", 303)
                return
            self._redirect(f"/admin/sources/{source_id}?notice=source_saved", 303)
            return

        if path == "/admin/sources":
            try:
                create_opds_source(
                    conn,
                    name=value("name"),
                    url=value("url"),
                    username=value("username"),
                    password=value("password"),
                    enabled=True,
                )
            except sqlite3.IntegrityError:
                self._redirect("/admin/sources?error=source_exists", 303)
                return
            except ValueError:
                self._redirect("/admin/sources?error=source", 303)
                return
            self._redirect("/admin/sources?notice=source_saved", 303)
            return

        if path == "/admin/opds/download":
            wants_json = self._wants_json() or value("ajax") == "1"
            try:
                source_id = int(value("source_id"))
            except ValueError:
                source_id = 0
            source = get_opds_source(conn, source_id)
            href = value("href")
            title = value("title")
            return_url = value("return_url")
            if not return_url.startswith("/admin/opds?"):
                return_url = "/admin/sources"
            if source is None or not href:
                if wants_json:
                    self._send_json(
                        {"ok": False, "error": "Источник или ссылка на книгу не найдены."},
                        status=400,
                    )
                else:
                    self._redirect(return_url + "&error=download", 303)
                return
            library = self.config.library_path
            if library is None:
                if wants_json:
                    self._send_json(
                        {"ok": False, "error": "Не задан каталог книг для скачивания."},
                        status=400,
                    )
                else:
                    self._redirect("/admin?error=no_library", 303)
                return
            try:
                result = download_fb2(
                    href,
                    library / "OPDS",
                    suggested_name=title,
                    username=source.username,
                    password=source.password,
                )
            except (OpdsError, OSError) as exc:
                LOGGER.warning("Ошибка скачивания OPDS: %s", exc)
                if wants_json:
                    self._send_json(
                        {"ok": False, "error": f"Не удалось скачать книгу: {exc}"},
                        status=502,
                    )
                else:
                    self._redirect(return_url + "&error=download", 303)
                return

            try:
                book = parse_fb2_file(result.path)
                book_id, _ = upsert_book(
                    conn,
                    book,
                    mtime=result.path.stat().st_mtime,
                    covers_dir=self.config.covers_dir,
                )
            except Fb2Error as exc:
                try:
                    record_parse_error(
                        conn,
                        result.path,
                        str(exc),
                        file_hash=result.sha256,
                        file_size=result.size,
                        mtime=result.path.stat().st_mtime,
                    )
                except Exception:
                    LOGGER.exception("Не удалось сохранить ошибку разбора OPDS-файла")
                if wants_json:
                    self._send_json(
                        {
                            "ok": False,
                            "error": "Файл скачан, но не распознан как FB2: " + str(exc),
                        },
                        status=422,
                    )
                else:
                    self._redirect(return_url + "&error=download_parse", 303)
                return
            except Exception:
                LOGGER.exception("Не удалось импортировать скачанную книгу")
                if wants_json:
                    self._send_json(
                        {"ok": False, "error": "Не удалось добавить скачанную книгу в каталог."},
                        status=500,
                    )
                else:
                    self._redirect(return_url + "&error=download", 303)
                return

            if wants_json:
                self._send_json(
                    {
                        "ok": True,
                        "message": "Книга скачана и добавлена в каталог.",
                        "book_id": book_id,
                        "book_url": f"/ui/books/{book_id}",
                    }
                )
            else:
                self._redirect(f"/admin/books/{book_id}?notice=downloaded", 303)
            return

        delete_match = _ADMIN_DELETE_PATH_RE.match(path)
        if delete_match:
            book_id = int(delete_match.group(1))
            if delete_books(conn, [book_id]) == 0:
                self._redirect("/admin/books?error=delete", 303)
            else:
                self._redirect("/admin/books?notice=deleted", 303)
            return

        edit_match = _ADMIN_BOOK_PATH_RE.match(path)
        if edit_match:
            book_id = int(edit_match.group(1))
            wants_json = self._wants_json() or value("ajax") == "1"
            if get_book(conn, book_id) is None:
                if wants_json:
                    self._send_json({"ok": False, "error": "Книга не найдена."}, status=404)
                else:
                    self._send_text("Книга не найдена", 404)
                return
            title = value("title")
            if not title:
                if wants_json:
                    self._send_json(
                        {"ok": False, "error": "Название книги не может быть пустым."},
                        status=400,
                    )
                else:
                    self._redirect(f"/admin/books/{book_id}?error=title", 303)
                return
            authors = [
                part.strip()
                for part in re.split(r"[,;]", value("authors"))
                if part.strip()
            ]
            genres = [
                part.strip()
                for part in re.split(r"[,;]", value("genres"))
                if part.strip()
            ]
            updated = update_book_metadata(
                conn,
                book_id,
                title=title,
                annotation=value("annotation"),
                lang=value("lang"),
                publisher=value("publisher"),
                city=value("city"),
                publish_year=value("publish_year"),
                isbn=value("isbn"),
                series=value("series"),
                series_number=value("series_number"),
                authors=authors,
                genres=genres,
            )
            save_and_close = value("action") == "save_close"
            if updated:
                if wants_json:
                    self._send_json(
                        {
                            "ok": True,
                            "message": "Изменения сохранены.",
                            "book_id": book_id,
                            "book_url": f"/ui/books/{book_id}",
                            "close": save_and_close,
                        }
                    )
                elif save_and_close:
                    self._redirect(f"/ui/books/{book_id}?notice=saved", 303)
                else:
                    self._redirect(f"/admin/books/{book_id}?notice=saved", 303)
            else:
                if wants_json:
                    self._send_json(
                        {"ok": False, "error": "Не удалось сохранить книгу."},
                        status=400,
                    )
                else:
                    self._redirect(f"/admin/books/{book_id}?error=book", 303)
            return

        if path == "/admin/archive-scan":
            library = self.config.library_path
            if library is None:
                self._redirect("/admin?error=no_library", 303)
                return
            directory = value("path")
            if not directory:
                self._redirect(
                    "/admin?" + urlencode({"error": "archive_scan", "archive_summary": "Не указан каталог с архивами."}),
                    303,
                )
                return
            try:
                date_from = date.fromisoformat(value("date_from")) if value("date_from") else None
                date_to = date.fromisoformat(value("date_to")) if value("date_to") else None
            except ValueError:
                self._redirect(
                    "/admin?" + urlencode({"error": "archive_scan", "archive_summary": "Неверный формат даты. Используйте ГГГГ-ММ-ДД."}),
                    303,
                )
                return
            types = [item for item in form.get("types", []) if item]
            try:
                report = scan_archives(
                    self.config.db_path,
                    directory,
                    file_types=types,
                    date_from=date_from,
                    date_to=date_to,
                    recursive="recursive" in form,
                    library_dir=library,
                    covers_dir=self.config.covers_dir,
                )
            except ArchiveError as exc:
                LOGGER.warning("Ошибка импорта архивов: %s", exc)
                self._redirect(
                    "/admin?" + urlencode({"error": "archive_scan", "archive_summary": str(exc)}),
                    303,
                )
                return
            except Exception as exc:
                LOGGER.exception("Ошибка импорта архивов")
                self._redirect(
                    "/admin?" + urlencode({"error": "archive_scan", "archive_summary": str(exc)}),
                    303,
                )
                return
            self._redirect(
                "/admin?" + urlencode(
                    {
                        "notice": "archive_scanned",
                        "archive_summary": report.summary(),
                    }
                ),
                303,
            )
            return

        if path == "/admin/scan":
            library = self.config.library_path
            if library is None:
                self._redirect("/admin?error=no_library", 303)
                return
            try:
                scan_library(
                    self.config.db_path,
                    library,
                    recursive="recursive" in form,
                    prune="prune" in form,
                    covers_dir=self.config.covers_dir,
                )
            except Exception:
                LOGGER.exception("Ошибка сканирования библиотеки")
                self._redirect("/admin?error=scan", 303)
                return
            self._redirect("/admin?notice=scanned", 303)
            return

        if path == "/admin/reindex":
            try:
                reindex_fts(conn)
            except Exception:
                LOGGER.exception("Ошибка перестроения FTS")
                self._redirect("/admin?error=reindex", 303)
                return
            self._redirect("/admin?notice=reindexed", 303)
            return

        self._send_text("Страница не найдена", 404)

    def _reader(self, conn, book_id: int, *, head: bool) -> None:
        body = web.reader_page(conn, book_id)
        if body is None:
            self._send_text("Книга не найдена", 404, head=head)
            return
        self._send_html(body, head=head)

    def _base_url(self) -> str:
        if self.config.public_url:
            return self.config.public_url.rstrip("/")
        host = self.headers.get("Host")
        if not host:
            address = self.server.server_address  # type: ignore[attr-defined]
            host = f"{address[0]}:{address[1]}"
        scheme = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip()
        if scheme not in {"http", "https"}:
            scheme = "http"
        return f"{scheme}://{host}"

    def _route(
        self,
        conn,
        path: str,
        query: Mapping[str, list[str]],
        *,
        base_url: str,
        page: int,
        page_size: int,
        head: bool,
    ) -> None:
        if path in {"", "/"}:
            accept = self.headers.get("Accept", "").lower()
            if "application/atom+xml" in accept or "application/opds" in accept:
                self._send_bytes(root_feed(conn, base_url), NAV_TYPE, head=head)
            else:
                notice = query.get("notice", [""])[0]
                error = query.get("error", [""])[0]
                self._send_html(
                    web.index_page(conn, notice=notice, error=error), head=head
                )
        elif path.startswith("/static/"):
            self._static_file(path[len("/static/"):], head=head)
        elif _READ_PATH_RE.match(path):
            self._reader(conn, int(_READ_PATH_RE.match(path).group(1)), head=head)  # type: ignore[union-attr]
        elif path == "/healthz":
            stats = get_stats(conn)
            body = json.dumps(
                {
                    "status": "ok",
                    "version": __version__,
                    "books": stats.books,
                    "failed": stats.failed,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", head=head)
        elif path == "/opds":
            self._send_bytes(root_feed(conn, base_url), NAV_TYPE, head=head)
        elif path == "/opds/opensearch.xml":
            self._send_bytes(
                opensearch_xml(base_url),
                "application/opensearchdescription+xml; charset=utf-8",
                head=head,
            )
        elif path == "/opds/authors":
            total = count_authors(conn)
            pages = _page_count(total, page_size)
            page = min(page, pages)
            authors, _ = list_authors(conn, offset=(page - 1) * page_size, limit=page_size)
            self._send_bytes(
                authors_feed(
                    conn,
                    base_url,
                    page=page,
                    page_size=page_size,
                    total=total,
                    authors=authors,
                ),
                NAV_TYPE,
                head=head,
            )
        elif _AUTHOR_PATH_RE.match(path):
            self._author(conn, int(_AUTHOR_PATH_RE.match(path).group(1)), base_url, page, page_size, head)  # type: ignore[union-attr]
        elif path == "/opds/genres":
            total = count_genres(conn)
            pages = _page_count(total, page_size)
            page = min(page, pages)
            genres, _ = list_genres(conn, offset=(page - 1) * page_size, limit=page_size)
            self._send_bytes(
                genres_feed(
                    conn,
                    base_url,
                    page=page,
                    page_size=page_size,
                    total=total,
                    genres=genres,
                ),
                NAV_TYPE,
                head=head,
            )
        elif _GENRE_PATH_RE.match(path):
            self._genre(conn, int(_GENRE_PATH_RE.match(path).group(1)), base_url, page, page_size, head)  # type: ignore[union-attr]
        elif path == "/opds/series":
            total = count_series(conn)
            pages = _page_count(total, page_size)
            page = min(page, pages)
            series, _ = list_series(conn, offset=(page - 1) * page_size, limit=page_size)
            self._send_bytes(
                series_feed(
                    conn,
                    base_url,
                    page=page,
                    page_size=page_size,
                    total=total,
                    series=series,
                ),
                NAV_TYPE,
                head=head,
            )
        elif _SERIES_PATH_RE.match(path):
            self._series(conn, int(_SERIES_PATH_RE.match(path).group(1)), base_url, page, page_size, head)  # type: ignore[union-attr]
        elif path == "/opds/books":
            books, total = list_books(
                conn, offset=(page - 1) * page_size, limit=page_size
            )
            pages = _page_count(total, page_size)
            if page > pages:
                page = pages
                books, total = list_books(
                    conn, offset=(page - 1) * page_size, limit=page_size
                )
            self._send_bytes(
                books_feed(
                    conn,
                    base_url,
                    page=page,
                    page_size=page_size,
                    total=total,
                    books=books,
                ),
                ACQ_TYPE,
                head=head,
            )
        elif _BOOK_PATH_RE.match(path):
            self._book(conn, int(_BOOK_PATH_RE.match(path).group(1)), base_url, head)  # type: ignore[union-attr]
        elif path == "/opds/search":
            search = (query.get("q", [""])[0] or "").strip()[:200]
            if not search:
                self._redirect("/opds")
                return
            books, total = list_books(
                conn,
                query=search,
                offset=(page - 1) * page_size,
                limit=page_size,
            )
            pages = _page_count(total, page_size)
            if page > pages:
                page = pages
                books, total = list_books(
                    conn,
                    query=search,
                    offset=(page - 1) * page_size,
                    limit=page_size,
                )
            self._send_bytes(
                search_feed(
                    conn,
                    base_url,
                    search,
                    page=page,
                    page_size=page_size,
                    total=total,
                    books=books,
                ),
                ACQ_TYPE,
                head=head,
            )
        elif _DOWNLOAD_PATH_RE.match(path):
            self._download(
                conn, int(_DOWNLOAD_PATH_RE.match(path).group(1)), head=head  # type: ignore[union-attr]
            )
        elif _COVER_PATH_RE.match(path):
            self._cover(
                conn, int(_COVER_PATH_RE.match(path).group(1)), head=head  # type: ignore[union-attr]
            )
        else:
            self._send_text("Не найдено", 404, head=head)

    def _author(self, conn, author_id: int, base_url: str, page: int, page_size: int, head: bool) -> None:
        author = get_author(conn, author_id)
        if author is None or author.books == 0:
            self._send_text("Автор не найден", 404, head=head)
            return
        pages = _page_count(author.books, page_size)
        page = min(page, pages)
        books, total = list_books(
            conn,
            author_id=author_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        self._send_bytes(
            author_books_feed(
                conn,
                base_url,
                author,
                page=page,
                page_size=page_size,
                total=total,
                books=books,
            ),
            ACQ_TYPE,
            head=head,
        )

    def _genre(self, conn, genre_id: int, base_url: str, page: int, page_size: int, head: bool) -> None:
        genre = get_genre(conn, genre_id)
        if genre is None or genre.books == 0:
            self._send_text("Категория не найдена", 404, head=head)
            return
        pages = _page_count(genre.books, page_size)
        page = min(page, pages)
        books, total = list_books(
            conn,
            genre_id=genre_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        self._send_bytes(
            genre_books_feed(
                conn,
                base_url,
                genre,
                page=page,
                page_size=page_size,
                total=total,
                books=books,
            ),
            ACQ_TYPE,
            head=head,
        )

    def _series(self, conn, series_id: int, base_url: str, page: int, page_size: int, head: bool) -> None:
        series = get_series(conn, series_id)
        if series is None or series.books == 0:
            self._send_text("Серия не найдена", 404, head=head)
            return
        pages = _page_count(series.books, page_size)
        page = min(page, pages)
        books, total = list_books(
            conn,
            series_id=series_id,
            offset=(page - 1) * page_size,
            limit=page_size,
            sort="series",
        )
        self._send_bytes(
            series_books_feed(
                conn,
                base_url,
                series,
                page=page,
                page_size=page_size,
                total=total,
                books=books,
            ),
            ACQ_TYPE,
            head=head,
        )

    def _book(self, conn, book_id: int, base_url: str, head: bool) -> None:
        row = conn.execute(
            "SELECT parse_error FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if row is None or row["parse_error"]:
            self._send_text("Книга не найдена", 404, head=head)
            return
        book = get_book(conn, book_id)
        if book is None:
            self._send_text("Книга не найдена", 404, head=head)
            return
        self._send_bytes(single_book_feed(conn, base_url, book), ACQ_TYPE, head=head)

    def _download(self, conn, book_id: int, *, head: bool) -> None:
        row = conn.execute(
            "SELECT file_path, file_name, format FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if row is None:
            self._send_text("Книга не найдена", 404, head=head)
            return
        file_path = Path(row["file_path"])
        if not file_path.is_file():
            self._send_text("Файл книги отсутствует", 404, head=head)
            return

        fmt = str(row["format"] or "fb2")
        name = str(row["file_name"] or file_path.name)
        try:
            data, inner_name, content_type = read_book_payload(file_path, fmt, name)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Не удалось прочитать файл книги %s: %s", file_path, exc)
            self._send_text("Не удалось прочитать файл книги", 500, head=head)
            return
        name = inner_name or name
        ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or f"book.{fmt}"
        disposition = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(name, safe='')}"
        )
        size = len(data)

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disposition)
        self.send_header("Accept-Ranges", "none")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not head:
            self.wfile.write(data)


    def _static_file(self, name: str, *, head: bool) -> None:
        types = {
            "style.css": "text/css; charset=utf-8",
            "app.js": "application/javascript; charset=utf-8",
            "reader.css": "text/css; charset=utf-8",
            "reader.js": "application/javascript; charset=utf-8",
        }
        content_type = types.get(name)
        if content_type is None:
            self._send_text("Файл не найден", 404, head=head)
            return
        path = Path(__file__).resolve().parent / "static" / name
        if not path.is_file():
            self._send_text("Файл не найден", 404, head=head)
            return
        stat = path.stat()
        etag = f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self._send_bytes(
            path.read_bytes(),
            content_type,
            head=head,
            extra_headers={
                "ETag": etag,
                "Cache-Control": "public, max-age=3600",
            },
        )

    def _cover(self, conn, book_id: int, *, head: bool) -> None:
        row = conn.execute(
            "SELECT cover_path, cover_mime FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if row is None or not row["cover_path"]:
            self._send_text("Обложка не найдена", 404, head=head)
            return
        cover = self.config.covers_dir / Path(row["cover_path"]).name
        if not cover.is_file():
            self._send_text("Обложка не найдена", 404, head=head)
            return
        stat = cover.stat()
        etag = f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        body = cover.read_bytes()
        self._send_bytes(
            body,
            row["cover_mime"] or "image/jpeg",
            head=head,
            extra_headers={"ETag": etag, "Cache-Control": "public, max-age=86400"},
        )

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: int = 200,
        head: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        headers = dict(extra_headers or {})
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if "Cache-Control" not in headers:
            self.send_header("Cache-Control", "no-cache")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_text(self, text: str, status: int, *, head: bool = False) -> None:
        self._send_bytes(
            (text + "\n").encode("utf-8"),
            "text/plain; charset=utf-8",
            status=status,
            head=head,
        )

    def _redirect(self, location: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


class OPDSHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: ServerConfig):
        self.config = config
        super().__init__(address, OPDSRequestHandler)


def create_server(host: str, port: int, config: ServerConfig) -> OPDSHTTPServer:
    """Создать (но не запускать) HTTP-сервер."""
    init_db(config.db_path)
    config.covers_dir.mkdir(parents=True, exist_ok=True)
    return OPDSHTTPServer((host, port), config)


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
