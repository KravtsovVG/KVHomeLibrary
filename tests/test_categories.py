from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from kvhomelibrary.categories import build_category_tree, category_root, split_category
from kvhomelibrary.db import (
    GenreRef,
    ParsedBook,
    connect_db,
    delete_books_for_genre,
    get_genre,
    init_db,
    list_genre_aliases,
    merge_genres,
    update_genre,
    upsert_book,
)
from kvhomelibrary.storage import NO_CATEGORY, UNKNOWN_LANGUAGE, target_path

from tests.support import WorkspaceTempDir


class CategoryTreeTests(unittest.TestCase):
    def test_split_by_first_underscore(self) -> None:
        self.assertEqual(split_category("Основная_Подкатегория"), ("Основная", "Подкатегория"))
        self.assertEqual(split_category("A_B_C"), ("A", "B_C"))
        self.assertEqual(split_category("БезРазделителя"), ("БезРазделителя", ""))
        self.assertEqual(category_root("Основная_Подкатегория"), "Основная")

    def test_tree_aggregates_books(self) -> None:
        genres = [
            GenreRef(id=1, code="a", name="Фантастика", books=2),
            GenreRef(id=2, code="a_b", name="Фантастика_Космос", books=3),
            GenreRef(id=3, code="c", name="Детективы", books=4),
        ]
        roots = build_category_tree(genres)
        self.assertEqual([item.name for item in roots], ["Детективы", "Фантастика"])
        fiction = roots[1]
        self.assertEqual(fiction.books, 5)
        self.assertEqual(fiction.children[0].name, "Фантастика_Космос")
        self.assertEqual(fiction.children[0].label, "Космос")


class GenreMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = WorkspaceTempDir()
        self.db = self.temp.path / "db.sqlite"
        init_db(self.db)
        self.conn = connect_db(self.db)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.temp.path, ignore_errors=True)

    def _book(self, name: str, genre: str, series: str = "") -> int:
        path = self.temp.path / f"{name}.fb2"
        path.write_text("x", encoding="utf-8")
        book = ParsedBook(
            title=name,
            sort_key=name.casefold(),
            genres=[genre],
            source_path=str(path),
            file_name=path.name,
            file_hash=f"hash-{name}",
            file_size=1,
        )
        book_id, _ = upsert_book(self.conn, book)
        return book_id

    def test_merge_moves_books_and_keeps_alias(self) -> None:
        self._book("one", "source")
        self._book("two", "target")
        with self.conn:
            self.conn.execute(
                "UPDATE genres SET name = 'A_B', category_path = 'A_B' WHERE code = 'source'"
            )
            self.conn.execute(
                "UPDATE genres SET name = 'C', category_path = 'C' WHERE code = 'target'"
            )
            source_id = self.conn.execute(
                "SELECT id FROM genres WHERE code = 'source'"
            ).fetchone()["id"]
            target_id = self.conn.execute(
                "SELECT id FROM genres WHERE code = 'target'"
            ).fetchone()["id"]

        moved, removed = merge_genres(self.conn, int(source_id), int(target_id))
        self.assertEqual((moved, removed), (1, 1))
        rows = self.conn.execute("SELECT genre_id FROM book_genres").fetchall()
        self.assertEqual({int(row["genre_id"]) for row in rows}, {int(target_id)})
        aliases = list_genre_aliases(self.conn)
        self.assertTrue(any(item["alias"] == "A_B" for item in aliases))

    def test_delete_category_removes_books(self) -> None:
        self._book("one", "category")
        genre_id = self.conn.execute(
            "SELECT id FROM genres WHERE code = 'category'"
        ).fetchone()["id"]
        self.assertEqual(delete_books_for_genre(self.conn, int(genre_id)), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"], 0)

    def test_move_and_display_name(self) -> None:
        self._book("one", "source")
        self._book("two", "target")
        with self.conn:
            self.conn.execute(
                "UPDATE genres SET category_path = 'A_B', name = 'A_B' WHERE code = 'source'"
            )
            self.conn.execute(
                "UPDATE genres SET category_path = 'C', name = 'C' WHERE code = 'target'"
            )
        genre_id = int(
            self.conn.execute("SELECT id FROM genres WHERE code = 'source'").fetchone()["id"]
        )
        update_genre(
            self.conn,
            genre_id,
            display_name="Космос",
            parent_path="C",
        )
        genre = get_genre(self.conn, genre_id)
        self.assertIsNotNone(genre)
        self.assertEqual(genre.category_path, "C_B")
        self.assertEqual(genre.display_name, "Космос")


class StoragePathTests(unittest.TestCase):
    def test_structure_and_fallbacks(self) -> None:
        book = ParsedBook(
            title="Книга",
            sort_key="книга",
            genres=["Основная_Подкатегория"],
            lang="ru",
            series="Серия",
            file_name="book.fb2",
            file_hash="abcdef1234567890",
        )
        path = target_path(Path("C:/library"), book, archive_zip=True)
        self.assertEqual(
            path,
            Path("C:/library/ru/Основная/Серия/book-abcdef1234.zip"),
        )

        unknown = ParsedBook(title="Без метаданных", sort_key="x", file_name="x.pdf")
        path = target_path(Path("C:/library"), unknown)
        self.assertEqual(
            path,
            Path(f"C:/library/{UNKNOWN_LANGUAGE}/{NO_CATEGORY}/x.pdf"),
        )
