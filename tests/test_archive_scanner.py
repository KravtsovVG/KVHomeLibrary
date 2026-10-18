from __future__ import annotations

import shutil
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from kvhomelibrary.archive_scanner import (
    ArchiveError,
    archive_created_date,
    iter_archive_entries,
    scan_archives,
)
from kvhomelibrary.book_formats import read_book_payload
from kvhomelibrary.db import connect_db, init_db

from tests.support import WorkspaceTempDir

ROOT = Path(__file__).resolve().parents[1]
FB2 = ROOT / "examples" / "books" / "bulgakov-belaya-gvardiya.fb2"


class ArchiveScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = WorkspaceTempDir()
        self.root = self.temp.path
        self.source = self.root / "source"
        self.library = self.root / "library"
        self.source.mkdir()
        self.library.mkdir()
        self.db = self.root / "library.db"
        init_db(self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _make_zip(self, name: str = "books.zip") -> Path:
        path = self.source / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("folder/book.fb2", FB2.read_bytes())
            archive.writestr(
                "docs/book.pdf",
                b"%PDF-1.4\n/Title (PDF Book)\n/Author (Author Name)\n%%EOF",
            )
            archive.writestr("empty.txt", b"ignore me")
        return path

    def test_import_zip_and_archive_added_books(self) -> None:
        self._make_zip()
        report = scan_archives(
            self.db,
            self.source,
            file_types=["fb2", "pdf"],
            library_dir=self.library,
            covers_dir=self.root / "covers",
        )
        self.assertEqual(report.archives_seen, 1)
        self.assertEqual(report.files_seen, 2)
        self.assertEqual(report.imported, 2)
        self.assertEqual(report.errors, [])

        conn = connect_db(self.db)
        rows = {
            row["format"]: row
            for row in conn.execute(
                "SELECT title, format, file_name, file_path FROM books"
            )
        }
        conn.close()
        self.assertEqual(rows["fb2"]["title"], "Белая гвардия")
        self.assertEqual(rows["pdf"]["title"], "PDF Book")
        self.assertEqual(rows["pdf"]["file_name"], "book.pdf")
        for row in rows.values():
            path = Path(row["file_path"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, self.library / "imported")
        payload, name, mime = read_book_payload(
            rows["pdf"]["file_path"], "pdf", rows["pdf"]["file_name"]
        )
        self.assertEqual(name, "book.pdf")
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(payload.startswith(b"%PDF"))

    def test_type_filter(self) -> None:
        self._make_zip()
        report = scan_archives(
            self.db,
            self.source,
            file_types=["pdf"],
            library_dir=self.library,
        )
        self.assertEqual(report.files_seen, 1)
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.skipped_types, 2)

    def test_date_filter(self) -> None:
        self._make_zip()
        future = date.today() + timedelta(days=2)
        report = scan_archives(
            self.db,
            self.source,
            file_types=["fb2"],
            date_from=future,
            library_dir=self.library,
        )
        self.assertEqual(report.archives_skipped_date, 1)
        self.assertEqual(report.files_seen, 0)

        past = archive_created_date(self.source / "books.zip") - timedelta(days=1)
        report = scan_archives(
            self.db,
            self.source,
            file_types=["fb2"],
            date_to=past,
            library_dir=self.library,
        )
        self.assertEqual(report.archives_skipped_date, 1)

    def test_rescan_updates_existing(self) -> None:
        self._make_zip()
        first = scan_archives(self.db, self.source, file_types=["fb2"], library_dir=self.library)
        second = scan_archives(self.db, self.source, file_types=["fb2"], library_dir=self.library)
        self.assertEqual(first.imported, 1)
        self.assertEqual(second.updated, 1)
        self.assertEqual(second.imported, 0)

    def test_invalid_archive_is_reported(self) -> None:
        bad = self.source / "bad.zip"
        bad.write_bytes(b"not a zip")
        report = scan_archives(self.db, self.source, file_types=["fb2"], library_dir=self.library)
        self.assertEqual(report.failed, 1)
        self.assertTrue(report.errors)

    def test_missing_7z_tool_is_an_error(self) -> None:
        with self.assertRaises(ArchiveError):
            list(iter_archive_entries(self.root / "not-found.7z"))
