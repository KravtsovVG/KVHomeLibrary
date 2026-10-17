from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from kvhomelibrary.db import (
    connect_db,
    count_authors,
    count_books,
    count_genres,
    count_series,
    delete_books,
    delete_missing,
    get_author,
    get_book,
    get_genre,
    get_series,
    get_stats,
    has_fts,
    init_db,
    list_authors,
    list_books,
    list_genres,
    list_series,
    record_parse_error,
    reindex_fts,
    upsert_book,
)
from kvhomelibrary.fb2 import parse_fb2_file
from tests.support import WorkspaceTempDir

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "books"
BULGAKOV = EXAMPLES / "bulgakov-belaya-gvardiya.fb2"
LUKYANENKO = EXAMPLES / "lukyanenko-nochnoy-dozor.fb2"
STRUGATSKIE = EXAMPLES / "strugatskie-piknik-na-obochine.fb2"


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = WorkspaceTempDir()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.path / "data" / "library.db"
        init_db(self.db)
        self.covers = self.db.parent / "covers"
        self.conn = connect_db(self.db)
        self.addCleanup(self.conn.close)

        self.ids: dict[str, int] = {}
        for path in (BULGAKOV, LUKYANENKO, STRUGATSKIE):
            book = parse_fb2_file(path)
            book_id, _ = upsert_book(self.conn, book, covers_dir=self.covers)
            self.ids[path.name] = book_id

    def test_counts(self) -> None:
        self.assertEqual(count_books(self.conn), 3)
        self.assertEqual(count_authors(self.conn), 4)
        self.assertEqual(count_genres(self.conn), 5)
        self.assertEqual(count_series(self.conn), 2)

        stats = get_stats(self.conn)
        self.assertEqual(stats.books, 3)
        self.assertEqual(stats.authors, 4)
        self.assertEqual(stats.genres, 5)
        self.assertEqual(stats.series, 2)
        self.assertEqual(stats.failed, 0)
        self.assertGreater(stats.total_size, 0)

    def test_covers_saved(self) -> None:
        files = list(self.covers.glob("*"))
        self.assertEqual(len(files), 2)
        book = get_book(self.conn, self.ids[BULGAKOV.name])
        self.assertIsNotNone(book)
        self.assertEqual(book.cover_mime, "image/png")  # type: ignore[union-attr]
        self.assertTrue((self.covers / (book.cover_path or "")).is_file())  # type: ignore[union-attr]

    def test_authors_genres_series(self) -> None:
        authors, total = list_authors(self.conn, limit=10)
        self.assertEqual(total, 4)
        self.assertEqual(len(authors), 4)
        names = {author.name for author in authors}
        self.assertIn("Сергей Васильевич Лукьяненко", names)
        self.assertIn("Аркадий Натанович Стругацкий", names)

        lukyanenko = next(author for author in authors if "Лукьяненко" in author.name)
        self.assertEqual(lukyanenko.books, 1)
        self.assertEqual(get_author(self.conn, lukyanenko.id).name, lukyanenko.name)  # type: ignore[union-attr]

        genres, genre_total = list_genres(self.conn, limit=20)
        self.assertEqual(genre_total, 5)
        fantasy = next(genre for genre in genres if genre.code == "sf_fantasy")
        self.assertEqual(fantasy.name, "Фэнтези")
        self.assertEqual(fantasy.books, 1)
        self.assertEqual(get_genre(self.conn, fantasy.id).code, "sf_fantasy")  # type: ignore[union-attr]

        series, series_total = list_series(self.conn, limit=20)
        self.assertEqual(series_total, 2)
        dozory = next(item for item in series if item.name == "Дозоры")
        self.assertEqual(dozory.books, 1)
        self.assertEqual(get_series(self.conn, dozory.id).name, "Дозоры")  # type: ignore[union-attr]

    def test_filters_and_pagination(self) -> None:
        all_books, total = list_books(self.conn, limit=2)
        self.assertEqual(total, 3)
        self.assertEqual(len(all_books), 2)
        second_page, total = list_books(self.conn, offset=2, limit=2)
        self.assertEqual(total, 3)
        self.assertEqual(len(second_page), 1)

        authors, _ = list_authors(self.conn, limit=10)
        lukyanenko = next(author for author in authors if "Лукьяненко" in author.name)
        books, total = list_books(self.conn, author_id=lukyanenko.id)
        self.assertEqual(total, 1)
        self.assertEqual(books[0].title, "Ночной Дозор")

        genres, _ = list_genres(self.conn)
        detective = next(genre for genre in genres if genre.code == "detective")
        books, total = list_books(self.conn, genre_id=detective.id)
        self.assertEqual(total, 1)
        self.assertEqual(books[0].title, "Ночной Дозор")

        series, _ = list_series(self.conn)
        dozory = next(item for item in series if item.name == "Дозоры")
        books, total = list_books(self.conn, series_id=dozory.id, sort="series")
        self.assertEqual(total, 1)
        self.assertEqual(books[0].series_number, "1")

    def test_search(self) -> None:
        if not has_fts(self.conn):
            self.skipTest("SQLite без FTS5")
        books, total = list_books(self.conn, query="дозор")
        self.assertEqual(total, 1)
        self.assertEqual(books[0].title, "Ночной Дозор")

        books, total = list_books(self.conn, query="Стругацкий")
        self.assertEqual(total, 1)
        self.assertEqual(books[0].title, "Пикник на обочине")

        books, total = list_books(self.conn, query="Турбиных")
        self.assertEqual(total, 1)
        self.assertEqual(books[0].title, "Белая гвардия")

        books, total = list_books(self.conn, query="нет-такой-книги")
        self.assertEqual(total, 0)

    def test_upsert_is_idempotent(self) -> None:
        book = parse_fb2_file(LUKYANENKO)
        book_id, action = upsert_book(self.conn, book, covers_dir=self.covers)
        self.assertEqual(action, "updated")
        self.assertEqual(book_id, self.ids[LUKYANENKO.name])
        self.assertEqual(count_books(self.conn), 3)
        self.assertEqual(len(list(self.covers.glob("*"))), 2)

    def test_moved_file_keeps_single_record(self) -> None:
        data = BULGAKOV.read_bytes().replace(
            "Белая гвардия".encode("utf-8"), "Белая гвардия (тест)".encode("utf-8")
        )
        original = self.tmp.path / "original.fb2"
        original.write_bytes(data)
        book = parse_fb2_file(original)
        book_id, action = upsert_book(self.conn, book, covers_dir=self.covers)
        self.assertEqual(action, "added")

        moved = self.tmp.path / "moved.fb2"
        shutil.move(original, moved)
        book = parse_fb2_file(moved)
        moved_id, action = upsert_book(self.conn, book, covers_dir=self.covers)
        self.assertEqual(moved_id, book_id)
        self.assertEqual(action, "updated")
        self.assertEqual(count_books(self.conn), 4)
        row = self.conn.execute(
            "SELECT file_path FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        self.assertEqual(Path(row["file_path"]), moved.resolve())

    def test_duplicate_copy_is_not_added(self) -> None:
        copy = self.tmp.path / "copy.fb2"
        shutil.copyfile(BULGAKOV, copy)
        book = parse_fb2_file(copy)
        book_id, action = upsert_book(self.conn, book, covers_dir=self.covers)
        self.assertEqual(book_id, self.ids[BULGAKOV.name])
        self.assertEqual(action, "duplicate")
        self.assertEqual(count_books(self.conn), 3)
        row = self.conn.execute(
            "SELECT file_path FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        self.assertEqual(Path(row["file_path"]), BULGAKOV.resolve())

    def test_delete_missing(self) -> None:
        removed = delete_missing(self.conn, [BULGAKOV, LUKYANENKO])
        self.assertEqual(removed, 1)
        self.assertEqual(count_books(self.conn), 2)
        self.assertEqual(count_authors(self.conn), 2)

    def test_parse_error_is_excluded(self) -> None:
        broken = self.tmp.path / "broken.fb2"
        broken.write_bytes(b"<FictionBook><description>")
        record_parse_error(
            self.conn,
            broken,
            "не удалось разобрать XML",
            file_hash="0" * 64,
            file_size=broken.stat().st_size,
            mtime=broken.stat().st_mtime,
        )
        stats = get_stats(self.conn)
        self.assertEqual(stats.books, 3)
        self.assertEqual(stats.failed, 1)
        books, total = list_books(self.conn)
        self.assertEqual(total, 3)
        self.assertEqual(len(books), 3)

    def test_delete_books(self) -> None:
        deleted = delete_books(self.conn, [self.ids[LUKYANENKO.name]])
        self.assertEqual(deleted, 1)
        self.assertEqual(count_books(self.conn), 2)
        self.assertIsNone(get_book(self.conn, self.ids[LUKYANENKO.name]))

    def test_reindex(self) -> None:
        if not has_fts(self.conn):
            self.skipTest("SQLite без FTS5")
        count = reindex_fts(self.conn)
        self.assertEqual(count, 3)
        books, total = list_books(self.conn, query="Зона")
        self.assertEqual(total, 1)
        self.assertEqual(books[0].title, "Пикник на обочине")


if __name__ == "__main__":
    unittest.main()
