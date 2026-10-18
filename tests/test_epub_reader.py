from __future__ import annotations

import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path

from kvhomelibrary.book_formats import build_book
from kvhomelibrary.db import connect_db, init_db, upsert_book
from kvhomelibrary.server import ServerConfig, create_server

from tests.support import WorkspaceTempDir

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Тестовый EPUB</dc:title>
    <dc:creator>Тестовый Автор</dc:creator>
    <dc:language>ru</dc:language>
    <dc:identifier id="id">urn:isbn:9780000000001</dc:identifier>
  </metadata>
  <manifest><item id="chapter" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Глава</title></head>
<body><h1>Глава первая</h1><p>Текст EPUB для проверки читалки.</p></body></html>
"""


def make_epub() -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr("OEBPS/chapter1.xhtml", CHAPTER)
    return buffer.getvalue()


class EpubReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = WorkspaceTempDir()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.path / "library.db"
        init_db(self.db)
        self.covers = self.tmp.path / "covers"
        self.library = self.tmp.path / "library"
        imported = self.library / "imported"
        imported.mkdir(parents=True)
        data = make_epub()
        target = imported / "epub-test.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("book.epub", data)
        conn = connect_db(self.db)
        self.book_id, _ = upsert_book(
            conn,
            build_book(data, "book.epub", "epub", str(target)),
            covers_dir=self.covers,
        )
        conn.close()

        self.httpd = create_server(
            "127.0.0.1",
            0,
            ServerConfig(
                db_path=self.db,
                covers_dir=self.covers,
                page_size=20,
                library_path=self.library,
            ),
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, str, bytes]:
        with urllib.request.urlopen(self.base + path, timeout=20) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()

    def test_epub_reader_page_assets_and_file(self) -> None:
        status, content_type, body = self._get(f"/read/{self.book_id}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        text = body.decode("utf-8")
        self.assertIn("epub.min.js", text)
        self.assertIn("jszip.min.js", text)
        self.assertIn("epub_reader.js", text)
        self.assertIn(f"/book-file/{self.book_id}", text)
        self.assertIn("Тестовый EPUB", text)

        status, content_type, payload = self._get(f"/book-file/{self.book_id}")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/epub+zip")
        self.assertTrue(payload.startswith(b"PK"))

        for asset in ("epub.min.js", "jszip.min.js", "epub_reader.js", "epub_reader.css"):
            status, _, data = self._get(f"/static/{asset}")
            self.assertEqual(status, 200, asset)
            self.assertTrue(data, asset)

    def test_epub_book_page_shows_reader(self) -> None:
        _, _, body = self._get(f"/ui/books/{self.book_id}")
        text = body.decode("utf-8")
        self.assertIn(f"/read/{self.book_id}", text)
        self.assertIn("Скачать EPUB", text)
