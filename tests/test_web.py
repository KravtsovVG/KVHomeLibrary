from __future__ import annotations

import base64
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kvhomelibrary.db import (
    connect_db,
    get_book,
    init_db,
    upsert_book,
)
from kvhomelibrary.fb2 import parse_fb2_file
from kvhomelibrary.server import ServerConfig, create_server
from tests.support import WorkspaceTempDir

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "books"
BULGAKOV = EXAMPLES / "bulgakov-belaya-gvardiya.fb2"
LUKYANENKO = EXAMPLES / "lukyanenko-nochnoy-dozor.fb2"
STRUGATSKIE = EXAMPLES / "strugatskie-piknik-na-obochine.fb2"


class WebUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = WorkspaceTempDir()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.path / "library.db"
        init_db(self.db)
        self.covers = self.tmp.path / "covers"

        self.conn = connect_db(self.db)
        self.addCleanup(self.conn.close)
        self.ids: dict[str, int] = {}
        for path in (BULGAKOV, LUKYANENKO, STRUGATSKIE):
            book = parse_fb2_file(path)
            book_id, _ = upsert_book(self.conn, book, covers_dir=self.covers)
            self.ids[path.name] = book_id

        self.httpd = create_server(
            "127.0.0.1",
            0,
            ServerConfig(
                db_path=self.db,
                covers_dir=self.covers,
                page_size=2,
                library_path=EXAMPLES,
            ),
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

        def stop() -> None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)

        self.addCleanup(stop)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _get(
        self, path: str, headers: dict[str, str] | None = None
    ) -> tuple[int, str, bytes]:
        request = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()

    def _post(
        self,
        path: str,
        fields: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, bytes]:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                **(headers or {}),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.geturl(), response.read()

    def test_pages_render(self) -> None:
        pages = [
            ("/", "Домашняя библиотека"),
            ("/ui", "Последние книги"),
            ("/ui/books", "Все книги"),
            ("/ui/authors", "Авторы"),
            ("/ui/genres", "Категории"),
            ("/ui/series", "Серии"),
            (f"/ui/books/{self.ids[BULGAKOV.name]}", "Белая гвардия"),
            ("/admin", "Администрирование"),
            ("/admin/books", "Администрирование книг"),
            (f"/admin/books/{self.ids[BULGAKOV.name]}", "Редактирование книги"),
        ]
        for path, marker in pages:
            status, content_type, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", content_type, path)
            self.assertIn(marker, body.decode("utf-8"), path)
            self.assertIn("KVHomeLibrary", body.decode("utf-8"), path)
        _, _, genres_body = self._get("/ui/genres")
        self.assertIn("синонимы:", genres_body.decode("utf-8"))
        _, _, book_body = self._get(f"/ui/books/{self.ids[BULGAKOV.name]}")
        self.assertIn("все книги серии →", book_body.decode("utf-8"))
        self.assertIn("series_id=", book_body.decode("utf-8"))

    def test_search_and_filters(self) -> None:
        _, _, body = self._get("/ui/books?" + urllib.parse.urlencode({"q": "Дозор"}))
        text = body.decode("utf-8")
        self.assertIn("Ночной Дозор", text)
        self.assertNotIn("Белая гвардия", text)

        author = self.conn.execute(
            "SELECT id FROM authors WHERE display_name LIKE '%Лукьяненко%'"
        ).fetchone()
        self.assertIsNotNone(author)
        _, _, body = self._get(
            "/ui/books?" + urllib.parse.urlencode({"author_id": author["id"]})
        )
        self.assertIn("Ночной Дозор", body.decode("utf-8"))
        self.assertNotIn("Пикник на обочине", body.decode("utf-8"))

        genre = self.conn.execute(
            "SELECT id FROM genres WHERE code = 'sf_fantasy'"
        ).fetchone()
        _, _, body = self._get(
            "/ui/books?" + urllib.parse.urlencode({"genre_id": genre["id"]})
        )
        self.assertIn("Ночной Дозор", body.decode("utf-8"))

        series = self.conn.execute(
            "SELECT id FROM series WHERE name = 'Дозоры'"
        ).fetchone()
        _, _, body = self._get(
            "/ui/books?" + urllib.parse.urlencode({"series_id": series["id"]})
        )
        self.assertIn("Ночной Дозор", body.decode("utf-8"))
        self.assertNotIn("Белая гвардия", body.decode("utf-8"))

        _, _, body = self._get("/ui/books?q=zzz-no-such-book")
        self.assertIn("Ничего не найдено", body.decode("utf-8"))

    def test_edit_book(self) -> None:
        book_id = self.ids[BULGAKOV.name]
        status, url, _ = self._post(
            f"/admin/books/{book_id}",
            {
                "title": "Белая гвардия (ред.)",
                "annotation": "Новая аннотация",
                "authors": "Михаил Булгаков",
                "genres": "prose_classic, history",
                "series": "Русская классика",
                "series_number": "1",
                "lang": "ru",
                "publisher": "Недра",
                "city": "Москва",
                "publish_year": "1925",
                "isbn": "978-5-00000-001-1",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("notice=saved", url)
        book = get_book(self.conn, book_id)
        self.assertIsNotNone(book)
        self.assertEqual(book.title, "Белая гвардия (ред.)")  # type: ignore[union-attr]
        self.assertIn("sci_history", [genre.code for genre in book.genres])  # type: ignore[union-attr]

        _, _, body = self._get("/ui/books?q=" + urllib.parse.quote("ред."))
        self.assertIn("Белая гвардия (ред.)", body.decode("utf-8"))

    def test_delete_book(self) -> None:
        book_id = self.ids[LUKYANENKO.name]
        status, _, _ = self._post(f"/admin/books/{book_id}/delete", {})
        self.assertEqual(status, 200)
        self.assertIsNone(get_book(self.conn, book_id))
        _, _, body = self._get("/ui/books")
        self.assertNotIn("Ночной Дозор", body.decode("utf-8"))

    def test_scan_action(self) -> None:
        status, url, _ = self._post("/admin/scan", {"recursive": "1"})
        self.assertEqual(status, 200)
        self.assertIn("notice=scanned", url)
        count = self.conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"]
        self.assertEqual(count, 3)

    def test_post_rejects_foreign_origin(self) -> None:
        book_id = self.ids[BULGAKOV.name]
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._post(
                f"/admin/books/{book_id}/delete",
                {},
                {"Origin": "http://evil.example"},
            )
        self.assertEqual(context.exception.code, 403)
        context.exception.close()
        self.assertIsNotNone(get_book(self.conn, book_id))

    def test_head_ui(self) -> None:
        request = urllib.request.Request(self.base + "/ui", method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")

    def test_ajax_edit_modal_and_save(self) -> None:
        book_id = self.ids[BULGAKOV.name]
        status, content_type, body = self._get(
            f"/admin/books/{book_id}?ajax=1",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        text = body.decode("utf-8")
        self.assertIn('id="edit-book-form"', text)
        self.assertNotIn("<!doctype html>", text)

        fields = {
            "title": "Белая гвардия (AJAX)",
            "annotation": "AJAX",
            "authors": "Михаил Булгаков",
            "genres": "prose_classic",
            "series": "Русская классика",
            "series_number": "1",
            "lang": "ru",
            "publisher": "Недра",
            "city": "Москва",
            "publish_year": "1925",
            "isbn": "978-5-00000-001-1",
            "action": "save_close",
        }
        status, _, body = self._post(
            f"/admin/books/{book_id}",
            fields,
            {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["close"])
        book = get_book(self.conn, book_id)
        self.assertEqual(book.title, "Белая гвардия (AJAX)")  # type: ignore[union-attr]

    def test_ajax_edit_validation_error(self) -> None:
        book_id = self.ids[BULGAKOV.name]
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._post(
                f"/admin/books/{book_id}",
                {"title": "", "action": "save_close"},
                {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            )
        self.assertEqual(context.exception.code, 400)
        payload = json.loads(context.exception.read())
        self.assertFalse(payload["ok"])
        context.exception.close()

    def test_static_css(self) -> None:
        status, content_type, body = self._get("/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", content_type)
        self.assertIn(b".book-card", body)

    def test_static_js(self) -> None:
        status, content_type, body = self._get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", content_type)
        self.assertIn(b"js-download-form", body)

    def test_reader_page(self) -> None:
        book_id = self.ids[BULGAKOV.name]
        status, content_type, body = self._get(f"/read/{book_id}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        text = body.decode("utf-8")
        self.assertIn("reader-flow", text)
        self.assertIn("Глава первая", text)
        self.assertIn("Велик был год", text)
        self.assertIn("reader.js", text)
        self.assertIn('data-reader-mode="single"', text)

    def test_reader_assets(self) -> None:
        for path, marker in (("/static/reader.css", b"column-fill"), ("/static/reader.js", b"layoutPages")):
            status, content_type, body = self._get(path)
            self.assertEqual(status, 200)
            self.assertIn(marker, body)


class AdminAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = WorkspaceTempDir()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.path / "library.db"
        init_db(self.db)
        conn = connect_db(self.db)
        try:
            book = parse_fb2_file(BULGAKOV)
            upsert_book(conn, book, covers_dir=self.tmp.path / "covers")
        finally:
            conn.close()

        self.httpd = create_server(
            "127.0.0.1",
            0,
            ServerConfig(
                db_path=self.db,
                covers_dir=self.tmp.path / "covers",
                library_path=EXAMPLES,
                admin_password="secret",
            ),
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

        def stop() -> None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)

        self.addCleanup(stop)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _request(self, path: str, auth: str = "") -> urllib.request.Request:
        headers = {"Authorization": auth} if auth else {}
        return urllib.request.Request(self.base + path, headers=headers)

    def test_admin_requires_basic_auth(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(self._request("/admin"), timeout=10)
        self.assertEqual(context.exception.code, 401)
        self.assertIn("Basic", context.exception.headers.get("WWW-Authenticate", ""))
        context.exception.close()

        token = base64.b64encode(b"admin:secret").decode("ascii")
        with urllib.request.urlopen(
            self._request("/admin", f"Basic {token}"), timeout=10
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("Администрирование", response.read().decode("utf-8"))

        wrong = base64.b64encode(b"admin:wrong").decode("ascii")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(self._request("/admin", f"Basic {wrong}"), timeout=10)
        self.assertEqual(context.exception.code, 401)
        context.exception.close()


if __name__ == "__main__":
    unittest.main()
