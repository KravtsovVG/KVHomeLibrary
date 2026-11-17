from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from kvhomelibrary.db import connect_db, init_db
from kvhomelibrary.organizer import organize_library

from tests.support import WorkspaceTempDir

ROOT = Path(__file__).resolve().parents[1]
FB2 = ROOT / "examples" / "books" / "bulgakov-belaya-gvardiya.fb2"


class OrganizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = WorkspaceTempDir()
        self.library = self.temp.path / "library"
        self.old = self.library / "old"
        self.old.mkdir(parents=True)
        shutil.copy2(FB2, self.old / "bulgakov.fb2")
        (self.old / "book.pdf").write_bytes(
            b"%PDF-1.4\n/Title (PDF Book)\n/Author (Author Name)\n%%EOF"
        )
        self.db = self.temp.path / "library.db"
        init_db(self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp.path, ignore_errors=True)

    def test_dry_run_does_not_move(self) -> None:
        events: list[tuple[int, int]] = []
        report = organize_library(
            self.db,
            self.library,
            dry_run=True,
            progress=lambda index, total, _path: events.append((index, total)),
        )
        self.assertEqual(report.total, 2)
        self.assertEqual(events, [(1, 2), (2, 2)])
        self.assertTrue((self.old / "bulgakov.fb2").exists())
        self.assertTrue((self.old / "book.pdf").exists())

    def test_moves_and_updates_db(self) -> None:
        report = organize_library(
            self.db,
            self.library,
            covers_dir=self.temp.path / "covers",
            prune_empty=True,
        )
        self.assertGreaterEqual(report.moved, 2)
        self.assertEqual(report.failed, 0)
        self.assertFalse((self.old / "bulgakov.fb2").exists())
        self.assertTrue(
            (
                self.library
                / "ru"
                / "Классическая проза"
                / "Русская классика"
            ).is_dir()
        )
        self.assertTrue(
            (self.library / "ЯзыкНеизвестен" / "БезЖанра").is_dir()
        )
        self.assertFalse(self.old.exists())
        conn = connect_db(self.db)
        try:
            rows = conn.execute("SELECT file_path FROM books ORDER BY id").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(Path(row["file_path"]).is_file())

        again = organize_library(self.db, self.library)
        self.assertEqual(again.moved, 0)
        self.assertEqual(again.already, 2)

    def test_copy_keeps_original(self) -> None:
        report = organize_library(
            self.db,
            self.library,
            copy=True,
        )
        self.assertGreaterEqual(report.copied, 2)
        self.assertTrue((self.old / "bulgakov.fb2").exists())
