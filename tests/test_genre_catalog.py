from __future__ import annotations

import shutil
import unittest

from kvhomelibrary.db import connect_db, init_db
from kvhomelibrary.genre_catalog import find_fb2_genre, normalize_fb2_genres

from tests.support import WorkspaceTempDir


class GenreCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = WorkspaceTempDir()
        self.db = self.temp.path / "library.db"
        init_db(self.db)
        self.conn = connect_db(self.db)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.temp.path, ignore_errors=True)

    def _insert(self, code: str, name: str) -> None:
        self.conn.execute(
            "INSERT INTO genres(code, name, sort_key) VALUES (?, ?, ?)",
            (code, name, name.casefold()),
        )
        self.conn.commit()

    def test_lookup_by_russian_name_and_synonym(self) -> None:
        self.assertEqual(find_fb2_genre("Фэнтези"), "sf_fantasy")
        self.assertEqual(find_fb2_genre("ужасы"), "sf_horror")
        self.assertEqual(find_fb2_genre("Боевая фантастика"), "sf_action")
        self.assertEqual(find_fb2_genre("fantasy"), "sf_fantasy")
        self.assertEqual(find_fb2_genre("неизвестный-жанр"), None)

    def test_normalize_renames_alias_code(self) -> None:
        self._insert("fantasy", "Фэнтези")
        report = normalize_fb2_genres(self.conn)
        self.assertEqual(report.renamed, 1)
        rows = self.conn.execute("SELECT code, name FROM genres").fetchall()
        self.assertEqual([(row["code"], row["name"]) for row in rows], [("sf_fantasy", "Фэнтези")])

    def test_normalize_merges_duplicate(self) -> None:
        self._insert("fantasy", "Фэнтези")
        self._insert("sf_fantasy", "Фэнтези")
        report = normalize_fb2_genres(self.conn)
        self.assertEqual(report.merged, 1)
        rows = self.conn.execute("SELECT code FROM genres").fetchall()
        self.assertEqual([row["code"] for row in rows], ["sf_fantasy"])

    def test_normalize_dry_run_does_not_change(self) -> None:
        self._insert("fantasy", "Фэнтези")
        report = normalize_fb2_genres(self.conn, dry_run=True)
        self.assertEqual(report.renamed, 1)
        row = self.conn.execute("SELECT code FROM genres").fetchone()
        self.assertEqual(row["code"], "fantasy")
