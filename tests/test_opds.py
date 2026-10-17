from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

from kvhomelibrary import opds
from kvhomelibrary.db import (
    connect_db,
    get_author,
    get_genre,
    get_series,
    init_db,
    list_authors,
    list_books,
    list_genres,
    list_series,
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
BASE = "http://testserver"


def _parse(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def _entries(root: ET.Element) -> list[ET.Element]:
    return root.findall(f"{{{opds.ATOM_NS}}}entry")


def _entry(root: ET.Element, title: str) -> ET.Element:
    for entry in _entries(root):
        if entry.findtext(f"{{{opds.ATOM_NS}}}title") == title:
            return entry
    raise AssertionError(f"нет entry с заголовком {title!r}")


def _links(elem: ET.Element, rel: str) -> list[ET.Element]:
    return [
        link
        for link in elem.findall(f"{{{opds.ATOM_NS}}}link")
        if link.get("rel") == rel
    ]


class OpdsFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = WorkspaceTempDir()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.path / "library.db"
        init_db(self.db)
        self.conn = connect_db(self.db)
        self.addCleanup(self.conn.close)
        self.covers = self.tmp.path / "covers"

        self.ids: dict[str, int] = {}
        for path in (BULGAKOV, LUKYANENKO, STRUGATSKIE):
            book = parse_fb2_file(path)
            book_id, _ = upsert_book(self.conn, book, covers_dir=self.covers)
            self.ids[path.name] = book_id

    def test_root_feed(self) -> None:
        root = _parse(opds.root_feed(self.conn, BASE))
        self.assertEqual(root.tag, f"{{{opds.ATOM_NS}}}feed")
        self.assertEqual(
            root.findtext(f"{{{opds.ATOM_NS}}}title"), "KVHomeLibrary"
        )
        entries = _entries(root)
        self.assertEqual(len(entries), 4)
        titles = {entry.findtext(f"{{{opds.ATOM_NS}}}title") for entry in entries}
        self.assertEqual(titles, {"Авторы", "Категории", "Серии", "Все книги"})

        self_link = _links(root, "self")[0]
        self.assertEqual(self_link.get("href"), BASE + "/opds")
        self.assertEqual(self_link.get("type"), opds.NAV_TYPE)
        self.assertTrue(_links(root, "search"))

        authors_entry = _entry(root, "Авторы")
        subsection = _links(authors_entry, "subsection")[0]
        self.assertEqual(subsection.get("href"), BASE + "/opds/authors")
        self.assertEqual(subsection.get("type"), opds.NAV_TYPE)

        books_entry = _entry(root, "Все книги")
        self.assertEqual(_links(books_entry, "subsection")[0].get("type"), opds.ACQ_TYPE)

    def test_authors_pagination(self) -> None:
        total = 4
        authors, _ = list_authors(self.conn, limit=2)
        root = _parse(
            opds.authors_feed(
                self.conn, BASE, page=1, page_size=2, total=total, authors=authors
            )
        )
        self.assertEqual(len(_entries(root)), 2)
        next_links = [link for link in _links(root, "next")]
        self.assertEqual(len(next_links), 1)
        self.assertIn("page=2", next_links[0].get("href", ""))
        self.assertIn("Всего авторов: 4", root.findtext(f"{{{opds.ATOM_NS}}}subtitle") or "")

        page2, _ = list_authors(self.conn, offset=2, limit=2)
        root2 = _parse(
            opds.authors_feed(
                self.conn, BASE, page=2, page_size=2, total=total, authors=page2
            )
        )
        self.assertEqual(len(_entries(root2)), 2)
        self.assertTrue(_links(root2, "previous"))

    def test_book_entries(self) -> None:
        books, total = list_books(self.conn, limit=50)
        feed = _parse(
            opds.books_feed(
                self.conn,
                BASE,
                page=1,
                page_size=50,
                total=total,
                books=books,
            )
        )
        self.assertEqual(len(_entries(feed)), 3)

        entry = _entry(feed, "Ночной Дозор")
        acquisition = _links(entry, "http://opds-spec.org/acquisition")
        self.assertEqual(len(acquisition), 1)
        self.assertEqual(acquisition[0].get("type"), opds.FB2_TYPE)
        self.assertEqual(
            acquisition[0].get("href"),
            f"{BASE}/download/{self.ids[LUKYANENKO.name]}",
        )
        self.assertTrue(_links(entry, "http://opds-spec.org/image"))
        self.assertTrue(_links(entry, "http://opds-spec.org/image/thumbnail"))

        categories = {
            category.get("term")
            for category in entry.findall(f"{{{opds.ATOM_NS}}}category")
        }
        self.assertEqual(categories, {"sf_fantasy", "detective"})
        self.assertEqual(entry.findtext(f"{{{opds.DC_NS}}}language"), "ru")
        self.assertEqual(entry.findtext(f"{{{opds.DC_NS}}}issued"), "1998")

        series = entry.find(f"{{{opds.OPDS_NS}}}series")
        self.assertIsNotNone(series)
        self.assertEqual(series.get("name"), "Дозоры")  # type: ignore[union-attr]
        self.assertEqual(series.get("number"), "1")  # type: ignore[union-attr]

        related = _links(entry, "related")
        self.assertTrue(related)
        self.assertIn("Все книги серии", related[0].get("title", ""))

    def test_author_feed(self) -> None:
        authors, _ = list_authors(self.conn, limit=10)
        author = next(item for item in authors if "Булгаков" in item.name)
        books, total = list_books(self.conn, author_id=author.id)
        feed = _parse(
            opds.author_books_feed(
                self.conn,
                BASE,
                author,
                page=1,
                page_size=50,
                total=total,
                books=books,
            )
        )
        self.assertEqual(len(_entries(feed)), 1)
        self.assertEqual(
            feed.findtext(f"{{{opds.ATOM_NS}}}title"), "Михаил Афанасьевич Булгаков"
        )
        self.assertEqual(_links(feed, "up")[0].get("href"), BASE + "/opds/authors")
        self.assertEqual(_links(feed, "self")[0].get("type"), opds.ACQ_TYPE)

    def test_genre_and_series_feeds(self) -> None:
        genres, _ = list_genres(self.conn)
        fantasy = next(item for item in genres if item.code == "sf_fantasy")
        books, total = list_books(self.conn, genre_id=fantasy.id)
        feed = _parse(
            opds.genre_books_feed(
                self.conn,
                BASE,
                fantasy,
                page=1,
                page_size=50,
                total=total,
                books=books,
            )
        )
        self.assertEqual(_entries(feed)[0].findtext(f"{{{opds.ATOM_NS}}}title"), "Ночной Дозор")
        self.assertEqual(feed.findtext(f"{{{opds.ATOM_NS}}}title"), "Фэнтези")

        series, _ = list_series(self.conn)
        dozory = next(item for item in series if item.name == "Дозоры")
        books, total = list_books(self.conn, series_id=dozory.id, sort="series")
        feed = _parse(
            opds.series_books_feed(
                self.conn,
                BASE,
                dozory,
                page=1,
                page_size=50,
                total=total,
                books=books,
            )
        )
        self.assertEqual(feed.findtext(f"{{{opds.ATOM_NS}}}title"), "Дозоры")
        self.assertEqual(len(_entries(feed)), 1)

    def test_search_feed(self) -> None:
        books, total = list_books(self.conn, query="Зона")
        feed = _parse(
            opds.search_feed(
                self.conn,
                BASE,
                "Зона",
                page=1,
                page_size=50,
                total=total,
                books=books,
            )
        )
        self.assertEqual(len(_entries(feed)), 1)
        self.assertEqual(
            _entries(feed)[0].findtext(f"{{{opds.ATOM_NS}}}title"),
            "Пикник на обочине",
        )
        self.assertIn("q=%D0%97%D0%BE%D0%BD%D0%B0", _links(feed, "self")[0].get("href", ""))

    def test_single_book_feed(self) -> None:
        books, _ = list_books(self.conn, query="Стругацкий")
        self.assertEqual(len(books), 1)
        feed = _parse(opds.single_book_feed(self.conn, BASE, books[0]))
        self.assertEqual(len(_entries(feed)), 1)
        self.assertEqual(feed.findtext(f"{{{opds.ATOM_NS}}}title"), "Пикник на обочине")

    def test_opensearch(self) -> None:
        root = _parse(opds.opensearch_xml(BASE))
        self.assertEqual(root.tag, f"{{{opds.OS_NS}}}OpenSearchDescription")
        url = root.find(f"{{{opds.OS_NS}}}Url")
        self.assertIsNotNone(url)
        self.assertIn("/opds/search?q={searchTerms}", url.get("template", ""))  # type: ignore[union-attr]
        self.assertEqual(url.get("type"), opds.ACQ_TYPE)  # type: ignore[union-attr]


class OpdsServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = WorkspaceTempDir()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.path / "library.db"
        init_db(self.db)
        self.covers = self.tmp.path / "covers"

        conn = connect_db(self.db)
        self.ids: dict[str, int] = {}
        try:
            for path in (BULGAKOV, LUKYANENKO, STRUGATSKIE):
                book = parse_fb2_file(path)
                book_id, _ = upsert_book(conn, book, covers_dir=self.covers)
                self.ids[path.name] = book_id
        finally:
            conn.close()

        config = ServerConfig(
            db_path=self.db, covers_dir=self.covers, page_size=2
        )
        self.httpd = create_server("127.0.0.1", 0, config)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

        def stop() -> None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)

        self.addCleanup(stop)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, dict(response.headers), response.read()

    def test_root_and_sections(self) -> None:
        status, headers, body = self._get("/opds")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], opds.NAV_TYPE)
        root = _parse(body)
        self.assertEqual(len(_entries(root)), 4)

        status, headers, body = self._get("/opds/authors")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], opds.NAV_TYPE)
        self.assertEqual(len(_entries(_parse(body))), 2)

        status, _, body = self._get("/opds/genres")
        self.assertEqual(status, 200)
        self.assertEqual(len(_entries(_parse(body))), 2)

        status, _, body = self._get("/opds/series")
        self.assertEqual(status, 200)
        self.assertEqual(len(_entries(_parse(body))), 2)

        status, headers, body = self._get("/opds/books")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], opds.ACQ_TYPE)
        feed = _parse(body)
        self.assertEqual(len(_entries(feed)), 2)
        self.assertTrue(_links(feed, "next"))

    def test_detail_and_download(self) -> None:
        book_id = self.ids[BULGAKOV.name]
        status, _, body = self._get(f"/opds/books/{book_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(_entries(_parse(body))), 1)

        status, headers, body = self._get(f"/download/{book_id}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], opds.FB2_TYPE)
        self.assertEqual(body, BULGAKOV.read_bytes())
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])

        status, headers, body = self._get(f"/covers/{book_id}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_search_and_health(self) -> None:
        query = urllib.parse.urlencode({"q": "Зона"})
        status, headers, body = self._get(f"/opds/search?{query}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], opds.ACQ_TYPE)
        self.assertEqual(len(_entries(_parse(body))), 1)

        status, headers, body = self._get("/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["books"], 3)

    def test_head_and_not_found(self) -> None:
        book_id = self.ids[BULGAKOV.name]
        request = urllib.request.Request(
            f"{self.base}/download/{book_id}", method="HEAD"
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.headers["Content-Length"]), BULGAKOV.stat().st_size)
            self.assertEqual(response.read(), b"")

        with self.assertRaises(urllib.error.HTTPError) as context:
            self._get("/opds/books/99999")
        self.assertEqual(context.exception.code, 404)
        context.exception.close()

    def test_landing_page(self) -> None:
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("KVHomeLibrary", body.decode("utf-8"))
        self.assertIn("/opds", body.decode("utf-8"))

    def test_root_content_negotiation(self) -> None:
        request = urllib.request.Request(
            self.base + "/",
            headers={"Accept": "application/atom+xml,application/xml;q=0.9"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], opds.NAV_TYPE)
            root = _parse(response.read())
        self.assertEqual(len(_entries(root)), 4)


if __name__ == "__main__":
    unittest.main()
