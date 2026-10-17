from __future__ import annotations

import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kvhomelibrary.db import (
    connect_db,
    create_opds_source,
    delete_opds_source,
    get_opds_source,
    init_db,
    list_opds_sources,
    mark_opds_source,
    update_opds_source,
)
from kvhomelibrary.server import ServerConfig, create_server
from tests.support import WorkspaceTempDir

ROOT = Path(__file__).resolve().parents[1]
BULGAKOV = ROOT / "examples" / "books" / "bulgakov-belaya-gvardiya.fb2"


class MockOpdsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        host = self.headers.get("Host", "127.0.0.1")
        base = f"http://{host}"
        if self.path == "/opds":
            body = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Тестовый OPDS</title>
  <id>urn:uuid:mock-feed</id>
  <link rel="self" href="{base}/opds"
        type="application/atom+xml;profile=opds-catalog;kind=navigation"/>
  <entry>
    <title>Книги</title>
    <id>urn:uuid:mock-books</id>
    <link rel="subsection" href="{base}/books"
          type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
  </entry>
</feed>""".encode("utf-8")
            self._send(body, "application/atom+xml; charset=utf-8")
        elif self.path == "/books":
            body = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opds="http://opds-spec.org/2010/catalog">
  <title>Книги тестового источника</title>
  <id>urn:uuid:mock-books</id>
  <link rel="search" href="{base}/search?q={{searchTerms}}"
        type="application/atom+xml"/>
  <entry>
    <title>Белая гвардия (OPDS)</title>
    <id>urn:uuid:mock-book-1</id>
    <author><name>Михаил Булгаков</name></author>
    <content type="html">&lt;p&gt;Роман из &lt;b&gt;тестового OPDS&lt;/b&gt;.&lt;/p&gt;</content>
    <opds:series name="Русская классика" number="1"/>
    <link rel="http://opds-spec.org/image" href="../cover.png" type="image/png"/>
    <link rel="related" href="{base}/author/96558"
          type="application/atom+xml" title="Все книги автора Волков Тим"/>
    <link rel="alternate" href="{base}/book/1" type="text/html"/>
    <link rel="http://opds-spec.org/acquisition" href="{base}/book.fb2"
          type="application/fb2+xml" length="{BULGAKOV.stat().st_size}"/>
  </entry>
</feed>""".encode("utf-8")
            self._send(body, "application/atom+xml; charset=utf-8")
        elif self.path == "/book.fb2":
            self._send(BULGAKOV.read_bytes(), "application/fb2+xml")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class SourceDatabaseTests(unittest.TestCase):
    def test_crud(self) -> None:
        with WorkspaceTempDir() as tmp:
            db = tmp / "library.db"
            init_db(db)
            conn = connect_db(db)
            try:
                source_id = create_opds_source(
                    conn,
                    name="Мой OPDS",
                    url="https://example.org/opds",
                    username="user",
                    password="pass",
                )
                sources = list_opds_sources(conn)
                self.assertEqual(len(sources), 1)
                source = get_opds_source(conn, source_id)
                self.assertIsNotNone(source)
                self.assertEqual(source.password, "pass")  # type: ignore[union-attr]

                self.assertTrue(
                    update_opds_source(conn, source_id, name="Новый OPDS")
                )
                source = get_opds_source(conn, source_id)
                self.assertEqual(source.name, "Новый OPDS")  # type: ignore[union-attr]
                self.assertEqual(source.password, "pass")  # type: ignore[union-attr]

                update_opds_source(conn, source_id, password="")
                self.assertEqual(get_opds_source(conn, source_id).password, "")  # type: ignore[union-attr]

                mark_opds_source(conn, source_id, error="HTTP 500")
                source = get_opds_source(conn, source_id)
                self.assertEqual(source.last_error, "HTTP 500")  # type: ignore[union-attr]
                self.assertTrue(source.last_checked_at)  # type: ignore[union-attr]

                with self.assertRaises(sqlite3.IntegrityError):
                    create_opds_source(
                        conn,
                        name="Дубль",
                        url="https://example.org/opds",
                    )

                self.assertTrue(delete_opds_source(conn, source_id))
                self.assertIsNone(get_opds_source(conn, source_id))
            finally:
                conn.close()


class SourceWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = WorkspaceTempDir()
        self.addCleanup(self.tmp.cleanup)
        self.library = self.tmp.path / "library"
        self.library.mkdir()
        self.db = self.tmp.path / "library.db"
        init_db(self.db)
        self.covers = self.tmp.path / "covers"

        self.mock = ThreadingHTTPServer(("127.0.0.1", 0), MockOpdsHandler)
        self.mock.daemon_threads = True
        self.mock_thread = threading.Thread(target=self.mock.serve_forever, daemon=True)
        self.mock_thread.start()
        self.addCleanup(self.mock.server_close)
        self.addCleanup(self.mock.shutdown)
        self.mock_base = f"http://127.0.0.1:{self.mock.server_address[1]}"

        self.conn = connect_db(self.db)
        self.addCleanup(self.conn.close)
        self.httpd = create_server(
            "127.0.0.1",
            0,
            ServerConfig(
                db_path=self.db,
                covers_dir=self.covers,
                library_path=self.library,
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

    def _get(self, path: str) -> tuple[int, str, bytes]:
        with urllib.request.urlopen(self.base + path, timeout=15) as response:
            return response.status, response.geturl(), response.read()

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

    def _create_source(self, url: str | None = None) -> int:
        self._post(
            "/admin/sources",
            {
                "name": "Тестовый OPDS",
                "url": url or (self.mock_base + "/opds"),
                "username": "",
                "password": "",
            },
        )
        row = self.conn.execute(
            "SELECT id FROM opds_sources ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row["id"])

    def test_browse_and_download(self) -> None:
        source_id = self._create_source()
        status, _, body = self._get(f"/admin/sources")
        self.assertEqual(status, 200)
        self.assertIn("Тестовый OPDS", body.decode("utf-8"))

        status, _, body = self._get(f"/admin/opds?source_id={source_id}")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("Тестовый OPDS", text)
        self.assertIn("Книги", text)

        books_url = self.mock_base + "/books"
        status, _, body = self._get(
            "/admin/opds?" + urllib.parse.urlencode(
                {"source_id": source_id, "url": books_url}
            )
        )
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("Белая гвардия (OPDS)", text)
        self.assertIn("Скачать в каталог", text)
        self.assertIn("<b>тестового OPDS</b>", text)
        self.assertNotIn("&lt;p&gt;", text)
        self.assertIn("все книги серии →", text)
        self.assertIn("Все книги автора Волков Тим", text)
        self.assertIn(f'src="{self.mock_base}/cover.png"', text)
        self.assertIn(f'href="{self.mock_base}/book/1"', text)

        return_url = "/admin/opds?" + urllib.parse.urlencode(
            {"source_id": source_id, "url": books_url}
        )
        status, final_url, body = self._post(
            "/admin/opds/download",
            {
                "source_id": str(source_id),
                "href": self.mock_base + "/book.fb2",
                "title": "Белая гвардия (OPDS)",
                "return_url": return_url,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("/admin/books/", final_url)
        self.assertIn("скачана", body.decode("utf-8").lower())
        row = self.conn.execute("SELECT title FROM books").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Белая гвардия")
        files = list((self.library / "OPDS").glob("*.fb2"))
        self.assertEqual(len(files), 1)
        source = get_opds_source(self.conn, source_id)
        self.assertIsNotNone(source)
        self.assertEqual(source.last_error, "")  # type: ignore[union-attr]

    def test_ajax_download(self) -> None:
        source_id = self._create_source()
        books_url = self.mock_base + "/books"
        return_url = "/admin/opds?" + urllib.parse.urlencode(
            {"source_id": source_id, "url": books_url}
        )
        status, _, body = self._post(
            "/admin/opds/download",
            {
                "source_id": str(source_id),
                "href": self.mock_base + "/book.fb2",
                "title": "Белая гвардия (OPDS)",
                "return_url": return_url,
            },
            {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertIn("book_url", payload)
        self.assertEqual(len(list((self.library / "OPDS").glob("*.fb2"))), 1)

    def test_ajax_download_error(self) -> None:
        source_id = self._create_source()
        with self.assertRaises(urllib.error.HTTPError) as context:
            self._post(
                "/admin/opds/download",
                {
                    "source_id": str(source_id),
                    "href": self.mock_base + "/missing.fb2",
                    "title": "Нет файла",
                    "return_url": "/admin/sources",
                },
                {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            )
        self.assertEqual(context.exception.code, 502)
        payload = json.loads(context.exception.read())
        self.assertFalse(payload["ok"])
        self.assertIn("скачать", payload["error"])
        context.exception.close()

    def test_bad_source_shows_error(self) -> None:
        source_id = self._create_source(self.mock_base + "/missing")
        status, _, body = self._get(f"/admin/opds?source_id={source_id}")
        self.assertEqual(status, 200)
        self.assertIn("HTTP 404", body.decode("utf-8"))
        source = get_opds_source(self.conn, source_id)
        self.assertIn("HTTP 404", source.last_error)  # type: ignore[union-attr]

    def test_edit_source(self) -> None:
        source_id = self._create_source()
        status, _, body = self._get(f"/admin/sources/{source_id}")
        self.assertEqual(status, 200)
        self.assertIn("Источник OPDS", body.decode("utf-8"))

        status, final_url, _ = self._post(
            f"/admin/sources/{source_id}",
            {
                "name": "Изменённый OPDS",
                "url": self.mock_base + "/opds",
                "username": "reader",
                "password": "",
                "enabled": "1",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("notice=source_saved", final_url)
        source = get_opds_source(self.conn, source_id)
        self.assertEqual(source.name, "Изменённый OPDS")  # type: ignore[union-attr]
        self.assertEqual(source.username, "reader")  # type: ignore[union-attr]

    def test_delete_source(self) -> None:
        source_id = self._create_source()
        status, final_url, _ = self._post(
            f"/admin/sources/{source_id}/delete", {}
        )
        self.assertEqual(status, 200)
        self.assertIn("/admin/sources", final_url)
        self.assertIsNone(get_opds_source(self.conn, source_id))


if __name__ == "__main__":
    unittest.main()
